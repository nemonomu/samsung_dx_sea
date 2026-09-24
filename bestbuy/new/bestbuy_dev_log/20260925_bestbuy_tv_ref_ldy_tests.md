# 2026-09-25 Best Buy TV / REF / LDY 복구 정책 수정

- 시간: 2026-09-25 00:42 KST (Asia/Seoul).
- 브랜치: `fix/bestbuy-collection-recovery`.
- 범위: browser_graphql main/BSR 목록, 상세·리뷰·비교상품, 후속 적재 허용 판정 및 메일 본문. 운영 수집/DB/S3/메일 발송은 실행하지 않음.
- 요청 기준: 목록 페이지 미수집은 전체 적재 차단 유지. 상세 실패 항목은 최초 포함 총 2회 요청 후 NULL 적재 허용 및 검수 메일. 페이지 경계 일부 SKU 중복은 첫 등장 채택.
- 검증 위치: `bestbuy/new`, 각 테스트의 격리된 임시 run root. 실제 운영 run root는 사용하지 않음.
- 명령: `python -m pytest tests -q -s -p no:cacheprovider --junitxml=<workspace>/task_logs/bby_recovery_20260925_all_tests.xml`.
- 조건: `PYTHONUTF8=1`; 테스트는 모의 browser GraphQL 응답·가상 대기 시계·모의 DB cursor 사용. 실사이트 요청/프록시/쿠키/유료 API 사용 없음.
- 최초 검증 장애: Windows sandbox의 `TemporaryDirectory` 접근/정리 권한 오류로 테스트가 실행되지 않음. 정상 권한으로 오프라인 테스트를 재실행함. pytest 기본 캡처의 닫힌 스트림 오류를 피하기 위해 `-s` 사용.
- 결과: JUnit 기준 210건, 실패 0, 오류 0, 생략 0; 테스트 시간 4.940초. 모의 HTTP 200/전송 오류/잘못된 응답을 검증했으며 실제 HTTP 상태나 실제 DB 적재 건수는 없음.
- 재현 사례: 비교상품 `subPlacements: null`; 리뷰 총수 15/본문 14; `buyingOptions` NOT_FOUND 및 리뷰 20개; 15페이지 18번째/16페이지 1번째 SKU 12372812 중복.
- 검증 결과: 누락 비교상품이 SQL INSERT 인자의 `None`으로 전달됨. 구매정보 오류에서도 목록 가격 $404.99/$674.99/$270 유지. 실패한 상세 항목만 2회 요청. main/BSR 실패는 적재 차단. 원본 위치 보존·첫 등장 채택. fullrun review 단계의 중복 재시도 방지.
- 변경 파일: `step00_collection_recovery.py`, `step01_listing_recovery.py`, `step08_collection_recovery.py`, `bestbuy_orchestrator.py`, `step16_email_notify.py`, `tests/test_collection_recovery.py`, `COLLECTION_RECOVERY.md`.
- 산출물: 임시 테스트 root의 상태/요청 증거/최종 CSV/부분 결과는 테스트 종료 시 정리. JUnit XML은 로컬 workspace `task_logs`에 보관하며 커밋에 포함하지 않음.
- 해석: 상세 데이터 부족을 경고로 유지하면서 적재 진행 가능. 목록 무결성·파일 저장·사용자 중단은 계속 보호. 다음 운영 실행에서 검수 필요 메일과 실제 DB 적재 건수를 확인할 것.
- 원격 확인: sandbox의 GitHub 접속이 로컬 프록시 연결 제한으로 실패해 승인된 정상 권한으로 원격 브랜치를 조회함. 원격 최신 변경을 보존한 뒤 푸시할 예정.
- 원격 기준점: `a94355f`는 로컬 시작점 `0cd69a4`와 Best Buy 파일 내용이 동일하며 Lowe's 변경만 추가됨. 이번 수정 커밋만 원격 최신 기준 위로 재배치하고 일반 push로 반영함.
