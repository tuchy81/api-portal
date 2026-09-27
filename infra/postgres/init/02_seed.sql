-- 시드 데이터: API 카탈로그 3종 + 승인된 신청 1건
-- 신 모델: api_catalog는 Public Base만 보유하고, 각 Endpoint(api_scope)가
-- 실제 호출할 Backend Full URL(upstream_url)을 직접 매핑한다.

INSERT INTO cdp.api_catalog (api_id, api_code, name, description, owner_dept, owner_sub, public_path, required_roles, status) VALUES
    ('a1000000-0000-0000-0000-000000000001', 'MDM-VENDOR',  'MDM 협력사 API',  'MDM 협력사 정보 조회/관리 API',  'MDM운영팀', 'u-test-002', '/capi/v1/vendors',   ARRAY['citizen-developer','mdm-reader'], 'PUBLISHED'),
    ('a1000000-0000-0000-0000-000000000002', 'FIN-ORDER',   '수주 현황 API',    '영업/수주 현황 조회 API',        '영업관리팀', 'u-test-002', '/capi/v1/orders',    ARRAY['citizen-developer'],              'PUBLISHED'),
    ('a1000000-0000-0000-0000-000000000003', 'HR-EMPLOYEE', 'HR 직원 API',      '인사 직원 정보 조회 API (제한적)', 'HR팀',      'u-test-002', '/capi/v1/employees', ARRAY['citizen-developer','hr-reader'],  'PUBLISHED');

INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, upstream_url, description) VALUES
    -- MDM-VENDOR: 목록/단건 조회 + 등록
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 목록 조회'),
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.read',  'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/vendors/{id}', '협력사 단건 조회'),
    ('a1000000-0000-0000-0000-000000000001', 'capi.vendor.write', 'POST', '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors',      '협력사 등록'),
    -- FIN-ORDER: 목록/단건 조회
    ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/orders',       '수주 목록 조회'),
    ('a1000000-0000-0000-0000-000000000002', 'capi.order.read',   'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/orders/{id}',  '수주 단건 조회'),
    -- HR-EMPLOYEE: 목록 조회만 제공
    ('a1000000-0000-0000-0000-000000000003', 'capi.hr.read',      'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/employees',    '직원 목록 조회');

-- 테스트용 승인된 신청 (u-test-001 → MDM-VENDOR)
INSERT INTO cdp.application (app_id, api_id, user_sub, user_name, dept_code, purpose, expected_tps, expected_daily, valid_until, status, requested_scopes, granted_scopes, reviewer_sub, reviewed_at) VALUES
    ('b2000000-0000-0000-0000-000000000001',
     'a1000000-0000-0000-0000-000000000001',
     'u-test-001', '홍길동', 'IT기획팀',
     '협력사 현황 주간 리포트 자동 생성',
     5, 2000, '2027-03-31',
     'APPROVED',
     ARRAY['capi.vendor.read'],
     ARRAY['capi.vendor.read'],
     'u-test-002', now());

-- 테스트용 PENDING 신청 (TC-P-01 테스트용)
INSERT INTO cdp.application (app_id, api_id, user_sub, user_name, dept_code, purpose, expected_tps, expected_daily, valid_until, status, requested_scopes, granted_scopes) VALUES
    ('b2000000-0000-0000-0000-000000000002',
     'a1000000-0000-0000-0000-000000000002',
     'u-test-001', '홍길동', 'IT기획팀',
     '수주 현황 자동 리포트',
     3, 1000, '2027-03-31',
     'PENDING',
     ARRAY['capi.order.read'],
     '{}');
