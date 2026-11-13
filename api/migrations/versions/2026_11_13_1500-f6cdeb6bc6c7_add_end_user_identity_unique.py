"""Add a unique constraint to the end-user identity columns.

Revision ID: f6cdeb6bc6c7
Revises: 8c2f5d7a1e30
Create Date: 2026-11-13 15:00:00

Concurrent first Service API requests for the same user can each insert
their own ``end_users`` row, because the lookup-then-insert in
``AppScopedEndUserService`` is not atomic and the table only has
non-unique indexes. The application code now inserts with
ON CONFLICT DO NOTHING, which needs a unique constraint to conflict on.

Upgrade order matters: existing duplicate rows would block the constraint,
so they are removed first. The earliest row per
(tenant_id, app_id, session_id, type) is kept, matching the deterministic
lookup order the repository now uses; conversations, messages, and message
feedback that pointed at a removed duplicate are re-pointed at the kept
row first. Only rows with a non-null app_id are touched: rows without an
app_id never conflict under the constraint (NULLs are distinct), so they
are left alone. ``upload_files.created_by`` is polymorphic (it also holds
account ids), so it is not re-pointed; a file created by a removed
duplicate identity keeps its value and simply stops resolving to a user.
"""

import sqlalchemy as sa
from alembic import op

revision = "f6cdeb6bc6c7"
down_revision = "8c2f5d7a1e30"
branch_labels = None
depends_on = None

_CONSTRAINT = "end_user_tenant_app_session_type_unique"
_REPOINT_TABLES = ("conversations", "messages", "message_feedbacks")


def _is_pg(conn):
    return conn.dialect.name == "postgresql"


def _keep_mapping_subquery():
    """Return the dup_id -> keep_id mapping for duplicate end users."""
    return """
        SELECT id AS dup_id,
               FIRST_VALUE(id) OVER (
                   PARTITION BY tenant_id, app_id, session_id, type
                   ORDER BY created_at ASC, id ASC
               ) AS keep_id
        FROM end_users
        WHERE app_id IS NOT NULL
    """


def _repoint_references(conn):
    mapping = _keep_mapping_subquery()
    for table in _REPOINT_TABLES:
        if _is_pg(conn):
            conn.execute(
                sa.text(f"""
                    UPDATE {table} AS t
                    SET from_end_user_id = m.keep_id
                    FROM ({mapping}) AS m
                    WHERE t.from_end_user_id = m.dup_id
                      AND m.dup_id <> m.keep_id
                """)
            )
        else:
            conn.execute(
                sa.text(f"""
                    UPDATE {table} t
                    INNER JOIN ({mapping}) m
                        ON t.from_end_user_id = m.dup_id
                       AND m.dup_id <> m.keep_id
                    SET t.from_end_user_id = m.keep_id
                """)
            )


def _delete_duplicates(conn):
    if _is_pg(conn):
        # Keep the earliest row per identity; DISTINCT ON groups NULL
        # app_id together, so the filter keeps those rows untouched.
        conn.execute(
            sa.text("""
                DELETE FROM end_users
                WHERE app_id IS NOT NULL
                  AND id NOT IN (
                      SELECT DISTINCT ON (tenant_id, app_id, session_id, type) id
                      FROM end_users
                      WHERE app_id IS NOT NULL
                      ORDER BY tenant_id, app_id, session_id, type, created_at ASC, id ASC
                  )
            """)
        )
    else:
        # MySQL: delete every row that has an earlier row for the same identity.
        conn.execute(
            sa.text("""
                DELETE t1 FROM end_users t1
                INNER JOIN end_users t2
                ON t1.tenant_id = t2.tenant_id
                   AND t1.app_id = t2.app_id
                   AND t1.session_id = t2.session_id
                   AND t1.type = t2.type
                   AND (t1.created_at > t2.created_at
                        OR (t1.created_at = t2.created_at AND t1.id > t2.id))
                WHERE t1.app_id IS NOT NULL
            """)
        )


def upgrade() -> None:
    conn = op.get_bind()
    _repoint_references(conn)
    _delete_duplicates(conn)

    with op.batch_alter_table("end_users", schema=None) as batch_op:
        batch_op.create_unique_constraint(_CONSTRAINT, ["tenant_id", "app_id", "session_id", "type"])


def downgrade() -> None:
    with op.batch_alter_table("end_users", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="unique")
