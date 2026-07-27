-- Query-optimization: index the API-key scope foreign keys that are filtered on
-- during scoped key lookups (api_keys.workflow_id / api_keys.webhook_trigger_id).
--
-- The model (models/api_key.py) now declares index=True on both columns, but the
-- alembic graph is broken in this project, so apply on existing databases with
-- this idempotent DDL instead of `alembic upgrade heads`.
--
-- Run:
--   docker exec writ-postgres psql -U writ -d writ \
--     -f - < backend/scripts/add_api_key_scope_indexes.sql
--
-- NOTE: api_keys is small, so a plain CREATE INDEX (brief ACCESS EXCLUSIVE lock)
-- is used. lock_timeout caps the wait so a stray "idle in transaction" connection
-- can never wedge the table. CREATE INDEX CONCURRENTLY was avoided on purpose:
-- it waits for ALL in-flight transactions and will hang indefinitely behind any
-- long-lived idle-in-transaction connection (observed in this environment).

SET lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS ix_api_keys_workflow_id
    ON api_keys (workflow_id);

CREATE INDEX IF NOT EXISTS ix_api_keys_webhook_trigger_id
    ON api_keys (webhook_trigger_id);
