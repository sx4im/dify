"""deduplicate end_users and add a unique constraint on their identity

Revision ID: c4d8e2a91f07
Revises: 8c2f5d7a1e30
Create Date: 2026-11-13 15:00:00.000000

Concurrent first Service API requests for the same user could each insert
their own end_users row: the lookup and the insert ran in separate sessions
with only non-unique indexes guarding the identity. The constraint makes
one row per (tenant_id, app_id, session_id, type) the database-enforced
invariant; AppScopedEndUserRepo.create relies on it to converge on the
winning row instead of raising.

Deployments that already carry duplicates cannot take the constraint as-is,
so the upgrade first drops every duplicate row except the earliest one
(created_at, then id) for each identity. Rows deleted this way are the ones
no later request resolves to: lookups order by created_at, so the earliest
row is the one live traffic keeps using.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d8e2a91f07"
down_revision = "8c2f5d7a1e30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM end_users WHERE id IN ("
            " SELECT id FROM ("
            "  SELECT id, ROW_NUMBER() OVER ("
            "   PARTITION BY tenant_id, app_id, session_id, type"
            "   ORDER BY created_at ASC, id ASC"
            "  ) AS rn FROM end_users"
            " ) AS end_user_duplicates WHERE rn > 1)"
        )
    )
    op.create_unique_constraint(
        "end_user_tenant_app_session_type_uq",
        "end_users",
        ["tenant_id", "app_id", "session_id", "type"],
    )


def downgrade() -> None:
    op.drop_constraint("end_user_tenant_app_session_type_uq", "end_users", type_="unique")
