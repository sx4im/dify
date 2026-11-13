## What broke

Concurrent first Service API requests for the same new `user` value each created their own `end_users` row (10 concurrent requests created 10 rows). `AppScopedEndUserService.get_or_create_end_user_by_type` does lookup-then-insert in separate sessions, and the table only had non-unique indexes, so nothing made the pair atomic. Later requests resolved to one of the duplicates, leaving the other rows orphaned.

## What changed

- Added a unique constraint on `(tenant_id, app_id, session_id, type)` to `EndUser`, plus a migration that re-points conversations, messages, and message feedback from removed duplicates to the earliest row, deletes the duplicates, and then creates the constraint.
- `AppScopedEndUserRepo.create` and `create_batch` now insert with ON CONFLICT DO NOTHING (Postgres/MySQL/SQLite, following the existing `human_input_v2` pattern) and return the stored row, so a lost race resolves to the single winner instead of a duplicate.
- `find_by_session` and `find_by_apps` now order by `created_at, id`, so databases that still hold legacy duplicates resolve to the same row every time.

## How verified

- New unit tests: double `create`, double `create_batch`, and double service-level `get_or_create_end_user` all return the same row with exactly one row stored; `find_by_apps` returns rows oldest first.
- Existing `test_app_scoped_end_user_repository.py` and `test_app_scoped_end_user_service.py` suites pass; ruff check and format clean on the changed files.

Fixes #46267
