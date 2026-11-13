"""SQLAlchemy persistence adapter for app-scoped end users.

Invariant: at most one ``EndUser`` row exists per
(tenant_id, app_id, session_id, type), enforced by the
``end_user_tenant_app_session_type_unique`` unique constraint. The
service-level lookup-then-create is not atomic, so concurrent first
requests for the same user can both observe "not found"; ``create`` and
``create_batch`` therefore insert with ON CONFLICT DO NOTHING and return
the stored row, so a lost race resolves to the single winner instead of
a duplicate row.
"""

from collections.abc import Sequence
from typing import override
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from libs.datetime_utils import naive_utc_now
from models.enums import EndUserType
from models.model import EndUser
from services.app_scoped_end_user_query_service import AppScopedEndUserQuery
from services.app_scoped_end_user_service import AppScopedEndUserRepository
from services.entities.app_scoped_end_user_entities import (
    AppScopedEndUserRecord,
    NewAppScopedEndUser,
    StoredAppScopedEndUser,
)

_END_USER_IDENTITY_CONSTRAINT = "end_user_tenant_app_session_type_unique"


class AppScopedEndUserRepo(AppScopedEndUserQuery, AppScopedEndUserRepository[EndUser]):
    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @override
    def find_by_id(self, *, tenant_id: str, app_id: str, end_user_id: str) -> AppScopedEndUserRecord | None:
        statement = (
            select(
                EndUser.id,
                EndUser.tenant_id,
                EndUser.app_id,
                EndUser.type,
                EndUser.external_user_id,
                EndUser.name,
                EndUser._is_anonymous,
                EndUser.session_id,
                EndUser.created_at,
                EndUser.updated_at,
            )
            .where(
                EndUser.id == end_user_id,
                EndUser.tenant_id == tenant_id,
                EndUser.app_id == app_id,
            )
            .limit(1)
        )

        with self._session_factory() as session:
            row = session.execute(statement).one_or_none()

        if row is None:
            return None

        (
            record_id,
            record_tenant_id,
            record_app_id,
            end_user_type,
            external_user_id,
            name,
            is_anonymous,
            session_id,
            created_at,
            updated_at,
        ) = row
        return AppScopedEndUserRecord(
            id=record_id,
            tenant_id=record_tenant_id,
            app_id=record_app_id,
            type=end_user_type.value,
            external_user_id=external_user_id,
            name=name,
            is_anonymous=is_anonymous,
            session_id=session_id,
            created_at=created_at,
            updated_at=updated_at,
        )

    @override
    def find_by_session(
        self,
        *,
        tenant_id: str,
        app_id: str,
        user_id: str,
    ) -> Sequence[StoredAppScopedEndUser[EndUser]]:
        with self._session_factory() as session:
            end_users = session.scalars(
                select(EndUser)
                .where(
                    EndUser.tenant_id == tenant_id,
                    EndUser.app_id == app_id,
                    EndUser.session_id == user_id,
                )
                # Legacy databases may still hold duplicate rows until the
                # dedupe migration runs; resolve them deterministically so
                # the same row is returned on every call.
                .order_by(EndUser.created_at.asc(), EndUser.id.asc())
            ).all()
        return [self._stored(end_user) for end_user in end_users]

    @override
    def find_by_apps(
        self,
        *,
        tenant_id: str,
        app_ids: Sequence[str],
        user_id: str,
        type: str,
    ) -> Sequence[StoredAppScopedEndUser[EndUser]]:
        with self._session_factory() as session:
            end_users = session.scalars(
                select(EndUser)
                .where(
                    EndUser.tenant_id == tenant_id,
                    EndUser.app_id.in_(app_ids),
                    EndUser.session_id == user_id,
                    EndUser.type == EndUserType(type),
                )
                .order_by(EndUser.created_at.asc(), EndUser.id.asc())
            ).all()
        return [self._stored(end_user) for end_user in end_users]

    @override
    def create(self, command: NewAppScopedEndUser) -> StoredAppScopedEndUser[EndUser]:
        """Insert the end user, or return the row a concurrent insert won with.

        Precondition: ``command`` carries the full identity
        (tenant_id, app_id, session_id, type).
        Postcondition: exactly one row matches the identity afterwards, and
        the returned metadata describes that row. The identity invariant
        holds after success and after a lost race.
        """
        with self._session_factory.begin() as session:
            self._insert_or_ignore(session, self._values_for(command))
            end_user = self._fetch_identity(session, command)
            return self._stored(end_user)

    @override
    def create_batch(
        self,
        commands: Sequence[NewAppScopedEndUser],
    ) -> Sequence[StoredAppScopedEndUser[EndUser]]:
        """Insert each end user, returning the stored row for each command.

        Precondition: every command carries the full identity
        (tenant_id, app_id, session_id, type).
        Postcondition: the result has one entry per command, in command
        order, each describing the single stored row for that identity.
        Commands that race with a concurrent insert resolve to the winner.
        """
        if not commands:
            return []
        with self._session_factory.begin() as session:
            for command in commands:
                self._insert_or_ignore(session, self._values_for(command))
            return [self._stored(self._fetch_identity(session, command)) for command in commands]

    @override
    def update_type(self, end_user_id: str, type: str) -> StoredAppScopedEndUser[EndUser]:
        with self._session_factory.begin() as session:
            end_user = session.get(EndUser, end_user_id)
            if end_user is None:
                raise RuntimeError(f"End user {end_user_id} disappeared before it could be updated")
            end_user.type = EndUserType(type)
        return self._stored(end_user)

    @staticmethod
    def _stored(end_user: EndUser) -> StoredAppScopedEndUser[EndUser]:
        if end_user.id is None:
            raise RuntimeError("App-scoped end user must be flushed before it is returned")
        if end_user.app_id is None:
            raise RuntimeError("App-scoped end user must have an app ID")
        return StoredAppScopedEndUser(
            id=end_user.id,
            app_id=end_user.app_id,
            type=end_user.type.value,
            value=end_user,
        )

    @staticmethod
    def _values_for(command: NewAppScopedEndUser) -> dict[str, object]:
        now = naive_utc_now()
        return {
            "id": str(uuid4()),
            "tenant_id": command.tenant_id,
            "app_id": command.app_id,
            "type": EndUserType(command.type),
            "session_id": command.session_id,
            "external_user_id": command.external_user_id,
            "_is_anonymous": command.is_anonymous,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _insert_or_ignore(session: Session, values: dict[str, object]) -> None:
        """Insert one end-user row, doing nothing when the identity exists.

        Precondition: ``values`` carries the full identity
        (tenant_id, app_id, session_id, type) plus a fresh id.
        Postcondition: a row for the identity exists in the session's
        transaction; no error is raised when another transaction inserted
        the same identity first. On dialects without ON CONFLICT support
        the plain insert is used and a concurrent duplicate still raises.
        """
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql_insert(EndUser).values(**values)
            session.execute(statement.on_conflict_do_nothing(constraint=_END_USER_IDENTITY_CONSTRAINT))
        elif dialect_name == "mysql":
            statement = mysql_insert(EndUser).values(**values)
            session.execute(statement.on_duplicate_key_update(session_id=statement.inserted.session_id))
        elif dialect_name == "sqlite":
            statement = sqlite_insert(EndUser).values(**values)
            session.execute(
                statement.on_conflict_do_nothing(index_elements=["tenant_id", "app_id", "session_id", "type"])
            )
        else:
            # created_at/updated_at are init=False on the model, so they
            # cannot be passed to the constructor; the column server
            # defaults fill them in.
            fallback_values = {key: value for key, value in values.items() if key not in ("created_at", "updated_at")}
            end_user = EndUser(**fallback_values)
            session.add(end_user)
            session.flush()

    @staticmethod
    def _fetch_identity(session: Session, command: NewAppScopedEndUser) -> EndUser:
        """Return the stored row for an identity, oldest first.

        Precondition: a row for the identity exists in the session's
        transaction.
        Postcondition: the returned row matches the identity; when legacy
        duplicates exist it is the earliest created one.
        """
        end_user = session.scalars(
            select(EndUser)
            .where(
                EndUser.tenant_id == command.tenant_id,
                EndUser.app_id == command.app_id,
                EndUser.session_id == command.session_id,
                EndUser.type == EndUserType(command.type),
            )
            .order_by(EndUser.created_at.asc(), EndUser.id.asc())
        ).first()
        if end_user is None:
            raise RuntimeError(f"End user for session {command.session_id} was not stored for app {command.app_id}")
        return end_user
