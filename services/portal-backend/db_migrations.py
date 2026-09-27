"""Runtime schema migrations applied on portal-backend startup.

We keep migrations here (not just SQL under infra/postgres/migrations) so
that a `docker compose up` on an existing pgdata volume upgrades itself —
init/*.sql only runs when the volume is empty. Each migration is idempotent
and detects whether it still needs to run.
"""
import logging

import asyncpg

logger = logging.getLogger("portal.db_migrations")


async def _column_exists(db: asyncpg.Connection, table: str, column: str) -> bool:
    return bool(
        await db.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema='cdp' AND table_name=$1 AND column_name=$2
               )""",
            table,
            column,
        )
    )


async def _migrate_endpoint_upstream(db: asyncpg.Connection) -> None:
    """SA-CHG-CDP-API-001: move `upstream_url` from api_catalog (API-level) to
    api_scope (Endpoint-level). Skips itself once api_scope.upstream_url is
    NOT NULL and api_catalog.upstream_url is gone."""
    has_scope_upstream = await _column_exists(db, "api_scope", "upstream_url")
    has_catalog_upstream = await _column_exists(db, "api_catalog", "upstream_url")

    if has_scope_upstream and not has_catalog_upstream:
        # Already at target schema.
        return

    logger.info("running migration: endpoint-upstream model (SA-CHG-CDP-API-001)")

    async with db.transaction():
        if not has_scope_upstream:
            await db.execute("ALTER TABLE cdp.api_scope ADD COLUMN upstream_url VARCHAR(500)")

        # Seed APIs: hard-replace to the canonical per-endpoint shape (matches
        # init/02_seed.sql). Pre-migration these had wildcard scopes; losing
        # the wildcard is intentional since the new model requires explicit
        # endpoints (spec section 3.2).
        await db.execute(
            """DELETE FROM cdp.api_scope WHERE api_id IN (
                   'a1000000-0000-0000-0000-000000000001',
                   'a1000000-0000-0000-0000-000000000002',
                   'a1000000-0000-0000-0000-000000000003'
               )"""
        )
        await db.executemany(
            """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, upstream_url, description)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            [
                ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 목록 조회'),
                ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/vendors/{id}', '협력사 단건 조회'),
                ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.write', 'POST', '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 등록'),
                ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/orders',       '수주 목록 조회'),
                ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/orders/{id}',  '수주 단건 조회'),
                ('a1000000-0000-0000-0000-000000000003', 'capi.hr.read',      'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/employees',    '직원 목록 조회'),
            ],
        )

        # Best-effort backfill for any user-registered APIs. Reproduces the
        # old rewrite (/capi/vN(.*) -> upstream_path$1) by attaching everything
        # after the /capi/vN prefix onto the API's base upstream — so a scope
        # like `/capi/v1/vendors/{id}` under upstream `http://svc/internal/api/v1`
        # becomes upstream `http://svc/internal/api/v1/vendors/{id}` (resource
        # segment preserved). The new relative pattern strips the Public Base,
        # not the version prefix, since it must join back onto public_path.
        if has_catalog_upstream:
            await db.execute(
                r"""UPDATE cdp.api_scope s
                   SET upstream_url = COALESCE(
                           s.upstream_url,
                           c.upstream_url || regexp_replace(
                               COALESCE(substring(s.path_pattern FROM '^/capi/v\d+(.*)$'), s.path_pattern),
                               '/\*\*$', ''
                           )
                       ),
                       path_pattern = CASE
                           WHEN s.path_pattern LIKE c.public_path || '/%'
                               THEN COALESCE(NULLIF(regexp_replace(
                                   substring(s.path_pattern FROM length(c.public_path) + 1),
                                   '/\*\*$', ''
                               ), ''), '/')
                           WHEN s.path_pattern = c.public_path THEN '/'
                           ELSE s.path_pattern
                       END
                   FROM cdp.api_catalog c
                   WHERE s.api_id = c.api_id"""
            )

        await db.execute("ALTER TABLE cdp.api_scope ALTER COLUMN upstream_url SET NOT NULL")

        if has_catalog_upstream:
            await db.execute("ALTER TABLE cdp.api_catalog DROP COLUMN upstream_url")

    logger.info("migration completed: endpoint-upstream model")


async def run_all(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as db:
        await _migrate_endpoint_upstream(db)
