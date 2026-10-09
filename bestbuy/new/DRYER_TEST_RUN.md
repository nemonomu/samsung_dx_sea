# BBY DRYER 단독 API 테스트

`feature/bby-dryer`의 독립 실행 경로. 통합 BAT 연결은 전체 검수 후 진행.

## 1. 일반 300개 + BSR 100개 테스트

원격 서버에 이 브랜치의 변경 파일을 반영한 뒤 명령 프롬프트에서 실행:

```bat
cd /d C:\samsung_dx_sea\bestbuy\new
call bby_dryer_daily_task.bat
```

기본값은 `DRYER` 검색의 기본 정렬에서 건조기 300개, Best-Selling 정렬에서 건조기 100개.
각 목록의 건조기 개수가 부족하면 다음 페이지까지 API로 조회. 사이트에 그만큼 없으면 확인된 마지막 페이지까지 수집.
두 목록의 중복을 합치므로 상세 수집·적재는 최대 400개이며, 중복 수만큼 줄어듦.
세탁기·일체형·부품·헤어드라이어 등은 대상에서 제외하되 원래 검색 순위는 유지.

Python 경로가 다르면 `BESTBUY_PYTHON`에 실행 파일 경로를 지정.
기존 설정 로더의 DB_CONFIG와 BESTBUY_ZIP_CODE/BESTBUY_STORE_ID를 사용. 기본 위치는 10010/482.
브라우저 API 수집이므로 ZenRows API 키 설정을 필수로 검사하거나 ZenRows 요청을 실행하지 않음.
기존 BBY 모듈의 Python 의존성과 DrissionPage·Chrome은 필요.
Chrome 표시 설정은 기존 LDY 실행기와 동일하게 HEADLESS=0 사용. 상품 데이터는 API 응답에서만 읽음.
단독 BAT의 setlocal과 별도 Python 프로세스 안에서만 설정 적용. 부모 셸과 실행 중인 기존 크롤러의 설정은 바꾸지 않음.
출력·상세·브라우저 작업 폴더는 해당 DRYER 실행 아래로 고정하고 다른 실행의 경로나 명시적 접속 포트를 상속하지 않음.
기존 TV/REF/LDY 공통 소스·기존 BAT와 해당 운영 테이블은 변경 대상에 포함하지 않음.

## 2. API 수집 흐름과 중복 처리

목록 요청 준비·파싱·검증은 기존 LDY 함수를 재사용.
목록과 상세는 기존 BBY의 browser_graphql 연결·세션 복구 함수로 요청. 브라우저는 API 접속을 유지하는 데 사용.
상품 값은 API 응답에서만 읽으며, HTML 상품 수집·DOM 대체 수집·Features 팝업 클릭은 실행하지 않음.
목록 순서가 실패 후 섞이지 않도록 실패한 목록 수집 회차는 버리고 첫 페이지부터 다시 시작.
전체 모드는 정상 응답의 비어 있는 마지막 페이지를 확인해야 완료. 같은 페이지 반복이나 페이지 제한 도달은 실패.

중복 기준은 기존 LDY의 내부 SKU 또는 BSIN/item 식별키. URL에서 확인된 BSIN도 같은 기준에 포함.
일반 목록에 있던 상품은 한 대상을 유지하고 bsr_rank를 연결. BSR에만 있던 상품만 추가.
서로 다른 내부 SKU가 같은 상품으로 병합될 때 기존 대상의 정상 SKU를 자동 교체하지 않음.
main_rank는 기본 정렬의 중복 제거 순서, bsr_rank는 Best-Selling 일반 상품의 중복 제거 순서.
각 순위는 제품 필터 전 위치를 유지하므로 숫자가 건너뛰거나 300/100보다 커질 수 있음.

상세는 기본 5개씩 묶어 필요한 식별값·가격·리뷰 집계·스펙·description{short long}·features{description title}만 요청.
short·long·Features는 같은 요청에서 함께 받으며 설명별 추가 API 왕복은 없음. long이 누락된 불완전 응답은 성공으로 처리하지 않음.
별도 Syndigo Key Features 요청과 화면 렌더링 대체 수집은 추가하지 않음.
가격 입력 자료형은 기존 BBY와 동일한 ProductItemPriceInput을 사용.
배송 상세, 리뷰 본문 20개, 비교 상품·다른 판매자, 상품 마스터, S3·메일은 요청하거나 실행하지 않음.
성공 제품은 즉시 저장하고, 재시도에는 실패 제품만 포함. 식별값이 맞지 않는 응답은 적재하지 않음.
HTTP 429(요청 과다 응답)은 목록·상세 모두 즉시 중단하며, 같은 실행에서 추가 묶음이나 재시도를 요청하지 않음. 상세가 불완전하면 DB 적재도 생략.

## 3. 컬럼과 적재 규칙

public.ldy_dryer_retail_test만 사용. 요청한 20컬럼을 유지하고 id는 DB에서 생성.
테이블이 없으면 기존 LDY 정의의 해당 20컬럼 자료형·기본값으로 생성.
기존 테이블의 컬럼 구성과 id 자동 생성 여부가 다르면 중단하며 구조를 변경하지 않음.
sku는 제조사 모델번호, item은 BSIN. Best Buy 내부 SKU는 진단과 API 요청에 사용.
새 batch_id는 기존 BBY처럼 b_YYYYMMDD_HHMMSS. calendar_week는 w41 형태, 날짜는 초 단위.
빈값(NULL), 가격, 리뷰 집계, 평점 표기는 기존 BBY 함수 재사용. 4.0은 4, 4.5는 4.5.
리뷰 두 집계 컬럼은 기존 LDY와 동일하게 reviewInfo.reviewCount 사용. 확인된 0과 미응답은 구분.
가격은 같은 상품의 주 가격만 사용하며, 다른 판매자·설치 서비스·오픈박스 가격을 섞지 않음.

capacity는 Specifications의 Capacity/Dryer Capacity → 자체 설명/Features → 자체 제품명 순서로 보완하며 단위를 포함한 명시값 유지.
스펙에 값이 있으면 설명이나 제목의 다른 용량으로 바꾸지 않음. 앞의 Capacity가 비었어도 뒤에 같은 라벨의 값이 있으면 사용. 세탁기 용량은 제외.
loading_type은 건조기 자체 스펙 → 자체 short·long 설명/Features → 자체 제품명 순서로 보완하며 없으면 NULL.
제품명과 스펙·설명의 값이 다르면 스펙·설명을 우선. 상위 근거 자체가 충돌하면 제목으로 덮지 않고 NULL과 충돌 사유를 남김.
Matching Washer Type과 관련 세탁기의 washer/washing machine 설명에 있는 front/top load 표현은 제외. 사진으로 추정하지 않음.
Features에서 추출한 값은 기존 합의된 Frontload/Topload로 정규화하고 근거를 따로 저장.
API 오류·누락과 정상 응답에 정보가 없는 경우는 구분. 대상 실패가 남으면 DB 적재 중단.
같은 배치를 다시 적재할 때 해당 Bestbuy 배치만 트랜잭션으로 교체하여 중복 방지.

## 4. 결과 검수와 재실행

콘솔의 run_dir 아래에서 다음 공개 결과를 확인:

- output/final_output.csv: 지정한 20컬럼. id는 빈칸.
- output/attribute_evidence.json: 로딩 타입·용량의 출처 근거와 정보 부재·충돌 구분.
- output/failures.json: 실패 상품의 내부 SKU와 안전한 오류 코드.
- dryer_manifest.json: 일반/BSR 개수, 중복 개수, 고유 대상·성공·실패 개수, 배치 아이디와 DB 적재 여부.
- logs/dryer.log: 콘솔과 같은 단계·진행·재시도·오류 기록. 실행을 재개하면 이어서 기록.
- logs/dryer_events.jsonl: 같은 내용을 항목별로 저장한 분석용 로그.

초기 준비 → DB 사전 확인 → 일반 목록 → BSR 목록 → 상세 수집 → DB 적재 순서로 시작·완료와 경과 시간을 표시.
목록은 페이지·목표 대비 후보 개수, 상세는 성공 건수/전체 대상·진행률·실패·남은 개수를 표시.
페이지 수를 미리 알 수 없는 전체 수집 모드에서는 목록 진행률 대신 수집 개수를 표시.
대기 중에는 10초마다 현재 단계와 대기 시간을 출력. 재시도 대상·횟수·대기 시간을 별도로 기록.
브라우저 실패는 Chrome 시작·사이트 접속·페이지 확인·GraphQL 요청 단계를 구분.
오류의 예외 종류, 고정 원인 분류, 코드 파일명·함수·줄 번호를 기록하고 원본 오류 문장·헤더·쿠키는 저장하지 않음.
API 누락 필드, 식별값 불일치와 상품 필수 컬럼 누락을 구분. 미수집이 남으면 적재 생략 사유를 출력.
HTTP 400 등 실패 응답은 api_response_rejected에서 응답 형태·GraphQL 오류 개수·고정 오류 분류를 확인.
알 수 없는 자료형(unknown_type), 자료형 불일치(type_mismatch), 지원하지 않는 필드(unsupported_field), 잘못된 변수(invalid_variables) 등을 구분.
graphql_fields/graphql_types는 허용된 이름만 기록하며 오류 원문·응답 본문·확장 정보는 출력하거나 저장하지 않음.
첫 상세 묶음 요청에서 실패하면 stage_failed와 run_complete의 failure_stage=detail_request로 확인.
loading_type/capacity의 정상 정보 부재는 attribute_missing으로 기록하며 통신 실패와 구분.
run_complete의 elapsed_s는 해당 실행의 초기화부터 DB 적재 또는 실패까지의 누적 초. 이후 browser_cleanup 완료의 elapsed_s에서 브라우저 정리까지 포함한 시간을 확인할 수 있음.

첫 목록에서 browser_api_unavailable가 발생했다면 logs/dryer.log의 stage_failed, api_failed, run_complete 확인.
해당 코드는 브라우저 API 호출 내부의 예외를 의미하며, 그 자체로 사이트 차단이나 검색어 문제를 확정하지 않음.
LDY 목록은 검색 페이지에서 연결을 준비하며 현재 DRYER는 공통 상세 연결 함수로 홈페이지에서 준비함.
이 진입 차이는 아직 유지. 실제 실패 위치를 로그로 확인한 후 필요한 연결 경로 수정을 검토.

기본 300/100 결과를 DB 적재 없이 확인:

```bat
call bby_dryer_daily_task.bat --no-load
```

실패한 같은 API 실행을 재개:

```bat
call bby_dryer_daily_task.bat --main-limit 300 --bsr-limit 100 --resume "이전 콘솔의 run_dir 경로"
```

재개 시 동일 위치·일반/BSR 제한과 기존 배치 아이디 유지. 성공 제품의 수집 시각도 유지.
이전 렌더링 방식 및 short 전용 API 버전 2 실행 폴더는 재개하지 않음. short·long을 함께 받는 버전 3에서 새 실행 폴더를 생성해야 함.
새 실행은 새 목록·가격·리뷰를 수집하며 이전 배치의 상품 캐시를 재사용하지 않음.

소규모 재검수는 목표 수량을 명시해서 실행:

```bat
call bby_dryer_daily_task.bat --main-limit 20 --bsr-limit 10
```

```sql
SELECT item, sku, retailer_sku_name, final_sku_price, original_sku_price, savings,
       count_of_reviews, star_rating, loading_type, capacity, main_rank, bsr_rank
FROM public.ldy_dryer_retail_test
WHERE batch_id = '<dryer_manifest.json의 batch_id>'
ORDER BY main_rank NULLS LAST, bsr_rank NULLS LAST;
```

## 5. 전체 테스트

소수 제품 검수 후 두 목록의 전체 페이지를 수집:

```bat
call bby_dryer_daily_task.bat --main-limit 0 --bsr-limit 0
```

기존 LDY의 상위 300/100개 제한을 전체 DRYER 모드에 적용하지 않음.
기본 안전 제한은 각 목록 100페이지이며 제한에 도달하면 불완전 수집으로 중단.
Features API는 원격의 LG·Insignia·Electrolux 3개 제품에서 정상 응답을 확인.
수정된 전체 요청의 실제 목록·가격·DB 적재, 가스·소형 제품의 표기는 원격 실행 결과로 추가 검수 필요.
통합 BAT 연결은 실제 수집·적재 검수 후 진행.
