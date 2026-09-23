CREATE SCHEMA IF NOT EXISTS cdp;

-- 공개 API 카탈로그
CREATE TABLE cdp.api_catalog (
    api_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    api_code        VARCHAR(64)  NOT NULL UNIQUE,
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    owner_dept      VARCHAR(100) NOT NULL,
    owner_sub       VARCHAR(64)  NOT NULL,
    upstream_url    VARCHAR(500) NOT NULL,
    public_path     VARCHAR(200) NOT NULL UNIQUE,
    openapi_spec    JSONB,
    required_roles  TEXT[]       NOT NULL DEFAULT '{}',
    status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','PUBLISHED','DEPRECATED','RETIRED')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- API Scope 정의
CREATE TABLE cdp.api_scope (
    scope_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    api_id       UUID        NOT NULL REFERENCES cdp.api_catalog(api_id) ON DELETE CASCADE,
    scope_name   VARCHAR(100) NOT NULL,
    http_method  VARCHAR(10)  NOT NULL,
    path_pattern VARCHAR(300) NOT NULL,
    description  VARCHAR(300),
    UNIQUE (api_id, scope_name, http_method, path_pattern)
);

-- 사용 신청
CREATE TABLE cdp.application (
    app_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    api_id         UUID        NOT NULL REFERENCES cdp.api_catalog(api_id),
    user_sub       VARCHAR(64) NOT NULL,
    user_name      VARCHAR(100) NOT NULL,
    dept_code      VARCHAR(50),
    purpose        TEXT        NOT NULL,
    expected_tps   INT         NOT NULL DEFAULT 1,
    expected_daily INT         NOT NULL DEFAULT 1000,
    valid_until    DATE        NOT NULL,
    status         VARCHAR(30) NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','WITHDRAWN')),
    requested_scopes TEXT[]    NOT NULL DEFAULT '{}',
    granted_scopes TEXT[]      NOT NULL DEFAULT '{}',
    reviewer_sub   VARCHAR(64),
    review_comment TEXT,
    reviewed_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_app_user   ON cdp.application(user_sub, status);
CREATE INDEX idx_app_status ON cdp.application(status, created_at DESC);

-- PAT
-- token_hash: SHA256(secret) hex (64 chars). See services/portal-backend/pat_utils.py
--   for why plain SHA256 is safe here (256bit CSPRNG secret, no dictionary space).
-- token_hmac: HMAC-SHA256(server_key, secret) hex (64 chars). Kept in the DB so
--   /internal/pat/{token_id} can return it on a Redis cache miss — pat-auth.lua
--   verifies against this value even after eviction. Never expose to clients.
CREATE TABLE cdp.pat (
    token_id     VARCHAR(12)  PRIMARY KEY,
    app_id       UUID         NOT NULL REFERENCES cdp.application(app_id),
    user_sub     VARCHAR(64)  NOT NULL,
    token_name   VARCHAR(100) NOT NULL,
    token_hash   VARCHAR(64)  NOT NULL,
    token_hmac   VARCHAR(64)  NOT NULL,
    scopes       TEXT[]       NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE'
                 CHECK (status IN ('ACTIVE','REVOKED','EXPIRED','INACTIVE')),
    allowed_cidr TEXT[]       DEFAULT '{}',
    issued_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    revoked_by   VARCHAR(64),
    revoke_reason VARCHAR(300)
);
CREATE INDEX idx_pat_user    ON cdp.pat(user_sub, status);
CREATE INDEX idx_pat_expires ON cdp.pat(expires_at) WHERE status = 'ACTIVE';

-- 쿼터 정책
CREATE TABLE cdp.quota_policy (
    policy_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id       VARCHAR(12) NOT NULL UNIQUE REFERENCES cdp.pat(token_id) ON DELETE CASCADE,
    rate_limit_tps INT         NOT NULL DEFAULT 10,
    burst          INT         NOT NULL DEFAULT 20,
    daily_quota    INT         NOT NULL DEFAULT 5000,
    monthly_quota  INT         NOT NULL DEFAULT 100000,
    concurrency    INT         NOT NULL DEFAULT 5,
    updated_by     VARCHAR(64),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 감사 로그 (월 파티션)
CREATE TABLE cdp.audit_log (
    log_id       BIGSERIAL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type   VARCHAR(40) NOT NULL,
    token_id     VARCHAR(12),
    user_sub     VARCHAR(64),
    jwt_jti      VARCHAR(64),
    trace_id     VARCHAR(64),
    client_ip    INET,
    http_method  VARCHAR(10),
    request_path VARCHAR(500),
    status_code  INT,
    latency_ms   INT,
    error_code   VARCHAR(20),
    detail       JSONB,
    PRIMARY KEY (log_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE cdp.audit_log_2026_09 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE cdp.audit_log_2026_10 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE cdp.audit_log_2026_11 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE cdp.audit_log_2026_12 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE cdp.audit_log_2027_01 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');

-- 안전망: 월별 파티션 생성 배치(BAT-CDP-06)가 어떤 이유로든 밀려도 INSERT가 실패하지
-- 않도록 DEFAULT 파티션을 둔다. 배치는 여기 쌓인 행을 정상 파티션으로 옮기지 않으므로,
-- DEFAULT에 행이 쌓이고 있다면 배치가 동작하지 않는다는 신호로 봐야 한다.
CREATE TABLE cdp.audit_log_default PARTITION OF cdp.audit_log DEFAULT;

-- 사용량 집계 (일 단위)
CREATE TABLE cdp.usage_stat_daily (
    stat_date    DATE        NOT NULL,
    token_id     VARCHAR(12) NOT NULL,
    api_id       UUID        NOT NULL,
    call_count   BIGINT      NOT NULL DEFAULT 0,
    error_count  BIGINT      NOT NULL DEFAULT 0,
    avg_latency  INT         NOT NULL DEFAULT 0,
    p95_latency  INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (stat_date, token_id, api_id)
);
