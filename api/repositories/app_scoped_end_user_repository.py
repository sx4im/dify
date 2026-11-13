"""SQLAlchemy persistence adapter for app-scoped end users."""

from collections.abc import Sequence
from typing import override
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from models.enums import EndUserType
from models.model import EndUser
from services.app_scoped_end_user_query_service import AppScopedEndUserQuery
from services.app_scoped_end_user_service import AppScopedEndUserRepository
from services.entities.app_scoped_end_user_entities import (
    AppScopedEndUserRecord,
    NewAppScopedEndUser,
    StoredAppScopedEndUser,
)


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
                # Oldest first, so concurrent readers converge on the same row
                # while duplicates created before the unique constraint existed
                # are still being cleaned up.
                .order_by(EndUser.created_at, EndUser.id)
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
                .order_by(EndUser.created_at, EndUser.id)
            ).all()
        return [self._stored(end_user) for end_user in end_users]

    @override
    def create(self, command: NewAppScopedEndUser) -> StoredAppScopedEndUser[EndUser]:
        # Insert-or-fetch: two concurrent first requests for the same user both
        # reach this point after finding nothing. The unique constraint on
        # (tenant_id, app_id, session_id, type) makes one insert win; the
        # loser re-reads the winner instead of raising.
        values = self._values(command)
        with self._session_factory.begin() as session:
            self._insert_end_users(session, [values])
            stored = self._fetch_by_identity(
                session,
                tenant_id=command.tenant_id,
                app_id=command.app_id,
                session_id=command.session_id,
                type=command.type,
            )
        if stored is None:
            raise RuntimeError("App-scoped end user creation did not produce a row")
        return stored

    @override
    def create_batch(
        self,
        commands: Sequence[NewAppScopedEndUser],
    ) -> Sequence[StoredAppScopedEndUser[EndUser]]:
        if not commands:
            return []
        values_list = [self._values(command) for command in commands]
        with self._session_factory.begin() as session:
            self._insert_end_users(session, values_list)
            stored = [
                self._fetch_by_identity(
                    session,
                    tenant_id=command.tenant_id,
                    app_id=command.app_id,
                    session_id=command.session_id,
                    type=command.type,
                )
                for command in commands
            ]
        missing = [command for command, row in zip(commands, stored, strict=True) if row is None]
        if missing:
            raise RuntimeError("App-scoped end user batch creation did not produce rows")
        return [row for row in stored if row is not None]

    @staticmethod
    def _values(command: NewAppScopedEndUser) -> dict[str, object]:
        return {
            "id": str(uuid4()),
            "tenant_id": command.tenant_id,
            "app_id": command.app_id,
            "type": EndUserType(command.type),
            "_is_anonymous": command.is_anonymous,
            "session_id": command.session_id,
            "external_user_id": command.external_user_id,
        }

    @staticmethod
    def _insert_end_users(session: Session, values_list: Sequence[dict[str, object]]) -> None:
        """Insert rows, silently skipping ones another request just created.

        Same dialect split as the human-input contact repository: Postgres
        targets the unique constraint by name, MySQL re-assigns an identity
        column to its own inserted value (a no-op update), SQLite targets the
        identity columns. Unknown dialects fall back to a plain ORM insert.
        """
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql_insert(EndUser).values(values_list)
            session.execute(
                statement.on_conflict_do_nothing(
                    constraint="end_user_tenant_app_session_type_uq",
                )
            )
        elif dialect_name == "mysql":
            statement = mysql_insert(EndUser).values(values_list)
            session.execute(statement.on_duplicate_key_update(session_id=statement.inserted.session_id))
        elif dialect_name == "sqlite":
            statement = sqlite_insert(EndUser).values(values_list)
            session.execute(
                statement.on_conflict_do_nothing(index_elements=["tenant_id", "app_id", "session_id", "type"])
            )
        else:
            session.add_all(
                EndUser(
                    id=str(values["id"]),
                    tenant_id=str(values["tenant_id"]),
                    app_id=str(values["app_id"]) if values["app_id"] is not None else None,
                    type=EndUserType(str(values["type"])),
                    _is_anonymous=bool(values["_is_anonymous"]),
                    session_id=str(values["session_id"]),
                    external_user_id=(
                        str(values["external_user_id"]) if values["external_user_id"] is not None else None
                    ),
                )
                for values in values_list
            )
            session.flush()

    def _fetch_by_identity(
        self,
        session: Session,
        *,
        tenant_id: str,
        app_id: str,
        session_id: str,
        type: str,
    ) -> StoredAppScopedEndUser[EndUser] | None:
        end_user = session.scalar(
            select(EndUser)
            .where(
                EndUser.tenant_id == tenant_id,
                EndUser.app_id == app_id,
                EndUser.session_id == session_id,
                EndUser.type == EndUserType(type),
            )
            .order_by(EndUser.created_at, EndUser.id)
            .limit(1)
        )
        return self._stored(end_user) if end_user is not None else None

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
