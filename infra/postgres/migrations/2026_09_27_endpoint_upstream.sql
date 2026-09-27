-- Migration: endpoint-upstream model
-- Ticket:    SA-CHG-CDP-API-001
-- Date:      2026-09-27
--
-- Fresh installs pick this shape up from init/01_ddl.sql + init/02_seed.sql;
-- this file is what an EXISTING pgdata volume needs so the same restart
-- doesn't drop into a broken state (portal-backend expects
-- cdp.api_scope.upstream_url NOT NULL and no cdp.api_catalog.upstream_url).
--
-- portal-backend runs an idempotent version of this at startup
-- (services/portal-backend/db_migrations.py), so operators normally don't
-- have to apply it by hand. It's kept here as the human-readable reference.

BEGIN;

-- 1. Add the new column, nullable so backfill can proceed.
ALTER TABLE cdp.api_scope
    ADD COLUMN IF NOT EXISTS upstream_url VARCHAR(500);

-- 2. Seed APIs: hard-replace their scope rows so the shape matches init/02_seed.sql
--    exactly (Public Base carries the resource, each endpoint pattern is relative).
--    A dev volume that pre-dates this migration was populated by the old seed and
--    only has the wildcard rows below — losing the wildcard here is intentional
--    since the new model requires explicit endpoints (spec section 3.2).
DELETE FROM cdp.api_scope
WHERE api_id IN (
    'a1000000-0000-0000-0000-000000000001',
    'a1000000-0000-0000-0000-000000000002',
    'a1000000-0000-0000-0000-000000000003'
);

INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, upstream_url, description) VALUES
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 목록 조회'),
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/vendors/{id}', '협력사 단건 조회'),
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.write', 'POST', '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 등록'),
    ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/orders',       '수주 목록 조회'),
    ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/orders/{id}',  '수주 단건 조회'),
    ('a1000000-0000-0000-0000-000000000003', 'capi.hr.read',      'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/employees',    '직원 목록 조회');

-- 3. Best-effort backfill for any non-seed API. Reproduces the previous
--    APISIX rewrite /capi/vN(.*) -> upstream_path$1 so the new endpoint's
--    upstream keeps the resource segment: a scope like /capi/v1/vendors/{id}
--    on an API with upstream_url=http://svc/internal/api/v1 becomes
--    http://svc/internal/api/v1/vendors/{id} (a trailing /** wildcard is
--    stripped since the new endpoint model doesn't keep it).
UPDATE cdp.api_scope s
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
WHERE s.api_id = c.api_id
  AND c.upstream_url IS NOT NULL;

-- 4. Now every scope has an upstream — enforce it.
ALTER TABLE cdp.api_scope
    ALTER COLUMN upstream_url SET NOT NULL;

-- 5. Drop the API-level upstream (spec section 5.3: "신규 코드 전환 및 기존
--    데이터 마이그레이션 완료 후").
ALTER TABLE cdp.api_catalog
    DROP COLUMN IF EXISTS upstream_url;

COMMIT;
