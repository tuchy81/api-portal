# 통합 테스트 실행 가이드

## 사전 준비

```bash
# 1. 전체 시스템 기동
cd /home/tuchy/citizen-api-portal
docker-compose up -d

# 2. 서비스 헬스체크 대기 (약 30~60초)
docker-compose ps

# 3. 테스트 의존성 설치
cd tests
pip install -r requirements.txt
```

## 테스트 실행

```bash
# 통합 테스트 전체
pytest integration/ -v

# 보안 테스트
pytest security/ -v

# 전체 + 출력
pytest integration/ security/ -v -s

# 특정 TC만
pytest integration/test_tc_a.py -v
pytest integration/test_tc_q.py -v
pytest integration/test_tc_x.py -v
pytest integration/test_tc_p.py -v
pytest integration/test_full_workflow.py -v
```

## TC 목록

| TC ID | 파일 | 설명 |
|-------|------|------|
| TC-A-01 | test_tc_a.py | 유효 PAT + 허용 API → 200 + 쿼터 헤더 |
| TC-A-02 | test_tc_a.py | 폐기 PAT → 401 CDP-1001 |
| TC-A-03 | test_tc_a.py | 만료 PAT (Redis 조작) → 401 CDP-1001 |
| TC-A-04 | test_tc_a.py | Scope 외 경로 → 403 CDP-1003 |
| TC-A-05 | test_tc_a.py | CIDR 위반 → 403 CDP-1006 |
| TC-Q-01 | test_tc_q.py | 동시 15 req → 429 or 200 (burst 내) |
| TC-Q-02 | test_tc_q.py | 일일 쿼터 소진 → 429 CDP-1004 |
| TC-Q-03 | test_tc_q.py | 쿼터 리셋 → 200 복귀 |
| TC-X-01 | test_tc_x.py | JWT 캐시 히트 → 200 |
| TC-X-02 | test_tc_x.py | 서킷 OPEN + 캐시 없음 → 503 CDP-2001 |
| TC-X-03 | test_tc_x.py | JWT claims 검증 (aud/scope/sub/cdp_channel) |
| TC-P-01 | test_tc_p.py | 미승인 신청으로 PAT 발급 → 403 CDP-4003 |
| TC-P-02 | test_tc_p.py | PAT 한도 초과 → 409 CDP-4009 |
| TC-P-03 | test_tc_p.py | 폐기 즉시 반영 → 401 |
| E2E | test_full_workflow.py | 전체 플로우 |
| SEC-01 | test_security.py | PAT 평문 감사 로그 노출 0건 |
| SEC-02 | test_security.py | 헤더 스푸핑 차단 |
| SEC-03 | test_security.py | 원자 쿼터 카운터 정확성 |
| SEC-04 | test_security.py | Negative Cache 동작 |
