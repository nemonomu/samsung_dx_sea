# Lowe’s 건조기 테스트 실행

개발 브랜치는 `lowes-dryer`. 원격 저장소에 이 브랜치를 반영하고 전환한 다음 실행함.
아래 명령은 원격 PC의 저장소 루트 `C:\samsung_dx_sea` 기준임.

로컬 변경을 커밋·푸시해 `origin/lowes-dryer`가 게시된 뒤, 원격에서는 다음 순서로 반영함.
먼저 `git status`로 원격의 미반영 변경을 확인함. 변경이 있으면 처리 방식을 정한 뒤 전환함.

```powershell
cd C:\samsung_dx_sea
git status --short --branch
git fetch origin
git switch lowes-dryer
git pull --ff-only origin lowes-dryer
```

## 기본 수집·적재

```powershell
cd C:\samsung_dx_sea
.\lowes\lowes_dryer_daily_task.bat
```

기본값은 우편번호 **10010**, 메인 대상 최대 **300개**, Dryers BSR 표시 **1~100위**, 상품 동시 처리 **최대 2개**임.
메인은 Top Sellers(`sortBy_featured`), BSR은 `sortBy_bestSellers` API를 사용함.
기존 LDY의 매장 환경 설정을 재사용하고, 우편번호는 상속된 값과 관계없이 10010을 적용함.
기존 코드 기본 매장은 메인 1674, 상세 289임. `LOWES_API_STORE_ID`가 설정돼 있으면 양쪽에 적용함.
상세의 기존 대체 매장 설정도 재사용하고 실제 조회 매장은 사양 검수 자료에 기록함.

브라우저를 초기 접속한 뒤 같은 사이트 API로 목록·상세·리뷰 통계를 수집함.
상품 상세 페이지 이동, 리뷰 본문 여러 페이지, 추천·배송 API 수집은 필요 컬럼에 포함되지 않음.
로그에는 브라우저 버전·요청 지역·페이지·상세 진행률·오류·적재 결과가 표시됨.
기본 BAT은 `public.ldy_dryer_retail_test`에 적재함.
테이블 생성·변경, 다른 배치 삭제, 다른 크롤러 실행은 포함하지 않음.

메인·BSR 모두 **단독 건조기만** 포함하며 해당 제품의 Sponsored 광고도 포함함.
세탁기·건조기 세트, 건조기와 다른 상품의 묶음, 세탁건조 일체형, 단독 세탁기는 제외함.
실제 판매 상품의 이름·사양·분류·묶음 표시로 판단하며, 추천·호환·선택품·별매 문구만으로 제외하지 않음.
여러 SKU를 묶은 `/collections/` 카드도 제외하며 숫자 상품 ID가 없어도 목록 오류로 취급하지 않음.
목록에서 명확한 비대상은 상세·리뷰를 호출하지 않음. 나머지는 상세로 확인한 뒤 대상 순위를 확정함.
상세 응답은 캐시해 메인·BSR 공통 상품과 이후 값 수집에 재사용하며, 비대상은 리뷰를 호출하지 않음.
상세 실패 시 목록에 단독 건조기 근거가 있으면 확보한 값으로 유지함.
목록으로도 종류를 확인할 수 없는 상품은 제외 근거와 API 실패·미확인 사유를 남기며 일부 실패로 보고함.
상품 ID 중복은 첫 노출을 유지하고 중복·비대상 제거 후 목록별 연속 순위를 다시 부여함.
메인은 유효 상품 최대 300개를 채우거나 사이트 목록이 끝날 때까지 진행함.
BSR은 **표시 1~100위**만 확인한 뒤 중복·비대상을 제외함. 100개 미만이어도 101위 이후로 보충하지 않음.
`original_rank`·`original_bsr_rank` 및 JSONL `occurrences`로 제외 전 노출 순위를 추적할 수 있음.
공통 상품은 한 행에 두 순위를 저장함. 기본 한도에서 최종 행 수는 최대 400개임.
별도 환경변수 입력 없이 위 단독 BAT만 실행하면 됨.
수집 제한은 `LOWES_DRYER_MAIN_LIMIT`·`LOWES_DRYER_BSR_LIMIT` 설정으로 변경 가능함. 이미 설정된 값이 있으면 BAT 기본값보다 우선하므로 시작 로그의 `main_limit`·`bsr_limit`를 확인함.

## 상품 동시 처리와 안전성

BAT만 실행하면 `LOWES_DRYER_DETAIL_CONCURRENCY=2`가 기본 적용됨. 매회 설정 입력은 필요 없음.
설정값은 **1 또는 2**만 허용하며, 기존에 1이 설정돼 있으면 순차 처리함.
순차 처리로 비교할 때만 다음을 사용함. 비교 후 이 PowerShell 설정을 제거하면 BAT 기본값 2로 복귀함.

```powershell
$env:LOWES_DRYER_DETAIL_CONCURRENCY = '1'
.\lowes\lowes_dryer_daily_task.bat
Remove-Item Env:\LOWES_DRYER_DETAIL_CONCURRENCY
```

목록의 상세 대상 판정과 최종 상세·리뷰 단계에서 상품을 최대 2개까지 처리함.
상품 내부에서는 상세 응답 확보(기존 캐시 재사용 포함) → 리뷰 API 순서임.
먼저 끝난 작업의 자리에 다음 상품을 넣으며, 마지막 1개와 홀수 개수도 정상 처리함.
기존 `PAGE_WAIT`는 최종 처리 작업별 대기로 유지하고, 목록 페이지 대기·재시도 설정도 유지함.
메인 한도에 가까워지면 진행 중인 상품이 차지할 대상 수까지 예약해 불필요한 상세 요청을 막음.

Python 스레드를 늘려 Selenium을 공유하지 않음. 단일 Python 실행 흐름에서 브라우저 명령을 보내고,
브라우저 안의 API 요청만 겹치게 실행함. 요청마다 고유 번호로 응답을 회수함.
대체 매장 조회가 필요하면 진행 중인 요청을 마친 뒤 지역 설정을 바꾸고, 조회 후 원래 매장으로 복구함.
상품별 응답·재시도 상태는 분리하고, JSONL·CSV·로그는 한 흐름에서 기록함.
완료 순서와 관계없이 최종 CSV는 기존 상품 순서·메인/BSR 순위를 유지함.
진행 로그의 `[상세] 완료/전체`는 실제 처리 완료 건수이며 상품 ID가 표시됨.
중단 시 완료 상품과 이미 받은 상세 원본은 JSONL에 남고 진행 중인 브라우저 요청은 취소함.

시작 로그의 `detail_concurrency=2`, `[상세 시작] ... concurrency=2`와
`collection_summary.json`의 `settings.detail_concurrency`로 적용값을 확인함.
동시 처리로 전체 실행 시간이 얼마나 줄어드는지는 원격 수집 결과로 측정해야 함.

최종 컬럼은 요청한 20개임. `id`는 실제 테이블의 자동 생성 규칙을 따름.
텍스트 가격은 기존 LDY처럼 `$499` 형식이며, DB가 숫자형이면 적재 전에 숫자로 변환함.
용량은 명시된 `Dryer Capacity`만 사용하며, 공용 `Washer Dryer Capacity`는 NULL임.
사양에 단위가 명시되면 `Cu.Feet`로 표기를 정리하고 원본·단위·저장값을 검수 자료에 남김.
`Dryer Load Type`의 명시된 투입 방식을 사용하며 전기·가스를 투입 방식으로 대체하지 않음.
가격은 목록 우선·상세 보완, 리뷰·평점은 상세 리뷰 통계 우선·목록 보완임.
평가 수는 리뷰 수와 같으며, 실제 0과 미수집 NULL을 구분함.
음수 가격·리뷰 수, 소수 리뷰 수, 0~5 범위를 벗어난 평점은 사용하지 않고 검수 경고로 남김.
정상 모델명을 다른 식별값으로 교체하지 않고, 목록·상세 모델 불일치는 경고로 남김.
메인·BSR 모두 상세에서 비대상으로 확인되면 제외하고 리뷰 호출을 생략함.
API 응답에 다른 정보가 함께 올 수 있지만 배송·픽업·리뷰 본문 등을 별도로 추출·적재하지 않음.

## 결과와 로그 위치

```text
C:\samsung_dx_sea\lowes\data\dryer\<YYYYMMDD>\<batch_id>\
  logs\dryer.log
  manifest.json
  collection_summary.json
  db_load_summary.json              기본 적재가 성공한 경우 생성
  main\main_occurrences.csv
  main\main_targets.csv
  bsr\bsr_occurrences.csv
  bsr\bsr_rank_map.csv
  output\final_targets.csv
  output\final_output.csv           정확히 20개 컬럼, id는 자동 생성 전 빈칸
  audit\spec_audit.csv               사양명·원본값·정규화값·NULL 사유·조회 매장
  audit\excluded_items.jsonl         제외 상품 ID·SKU·사유·판정 원본·목록별 원래 순위·API 상태
  audit\value_audit.jsonl            상품 ID별 원본값·최종 20개 값·출처·정리 과정·NULL 사유
  audit\failures.json                최종 실패 API·HTTP 상태·재시도 수·영향 컬럼·실제 NULL 컬럼
  audit\quality_warnings.json        미분류·모델 불일치·사양 충돌 등
  audit\recovered_requests.json      대체 매장으로 복구한 요청
```

실행 시작 로그의 `run_root`와 `batch`를 기준으로 확인함.
`spec_audit.csv`의 `reason`은 `present`, `absent`, `shared_capacity_only`,
`listing_fallback`, `invalid_value`, `conflicting_values`, `collection_failure` 등을 구분함.
오류의 `attempts`는 총 시도 수, `retries`는 첫 요청 이후 추가 시도 수임.
실패 API의 영향 컬럼 중 목록에서 확보한 값은 유지될 수 있으므로 영향 컬럼 전부가 NULL이라는 의미는 아님.
`missing_columns`는 그 영향 컬럼 중 최종 CSV에서도 NULL인 컬럼임. 확보한 SKU·가격·리뷰 값은 이 목록에서 제외함.
API 응답 전체·쿠키·인증 헤더·DB 접속 값은 결과 파일이나 로그에 저장하지 않음.
JSONL은 현재 응답의 필요한 공개 상품값만 선택해 기록하며, 별도 쇼핑몰 API를 호출하지 않음.

종료 코드는 **0 정상**, **2 일부 실패·품질 경고**, **1 실패**, **130 중단**임.
유효 대상이 0개이면 헤더와 검수 파일을 저장하고 DB 적재를 생략함(`db_skip_reason=no_eligible_products`).
2여도 확보한 값은 테스트 테이블에 적재될 수 있음. `manifest.json`의 `db_loaded`로 적재 여부를 확인함.
메인 대상이 부족하면 실제 건수와 부족 사유를 기록함. 목록 호출 실패·반복 페이지 등은 정상 완료로 처리하지 않음.
BSR이 요청한 표시 순위에 도달하기 전에 비정상적으로 끝나면 실패로 보고함.

## JSONL 원본 대조

`audit\value_audit.jsonl`은 한 줄이 한 상품이며, 같은 상품 ID가 여러 줄로 중복되지 않음.
기존 CSV·JSON·로그는 계속 생성됨. JSONL 검수 자료가 DB 컬럼을 추가하지는 않음.

- `source_values`: 목록·상세 가격과 모델·리뷰 통계·관련 사양의 원본값. 메인·BSR 양쪽 원본도 연결함.
- `final_values`: 최종 CSV에 들어가는 20개 값. 자동 생성 전 `id`는 NULL임.
- `fields`: 컬럼마다 원본값, 사용한 출처, 정리 규칙, 최종값, NULL 사유를 보여줌.
- `api`: 목록·상세·리뷰 API의 상태와 시도 수. 리뷰 본문·헤더·응답 전체는 포함하지 않음.
- `classification`: 실제 판매 상품의 판정 결과·원본 근거·사용 출처.
- `region`: 요청 배송 ZIP·매장 ID와 응답의 공개 지역 항목.
- `price_checks`: 가격 항목 간 누락 조합. 기존 가격값을 변경하거나 계산하지 않음.

예를 들어 목록 가격이 499, 상세 가격이 599이면 두 값을 보관하고,
`final_sku_price`의 출처가 목록이며 최종값이 `$499`임을 확인할 수 있음.
리뷰 수는 상세 통계 우선·목록 보완이며, 실제 0은 NULL 사유 없이 0으로 기록됨.
일체형은 적재 대상에서 제외하며 원본 판정 근거는 제외 자료에 남김.
대상 건조기의 용량이 공용 사양으로만 제공되면 `shared_capacity_only` 사유와 최종 NULL을 기록함.
원천 부재·잘못된 값·수집 실패·가격 동일로 원래 가격 제거·목록에 없는 순위 등을 구분함.
국가·계정은 `fixed`, 순위·주차는 `derived`, 날짜·배치는 `run_metadata`, id는 `db_generated`로 구분함.

처리 중에도 파일을 저장하고, 임시 파일을 완성한 뒤 교체해 이전 정상 기록을 유지함.
다음 `status`를 구분해야 함.

- `pending_selection`: 목록·종류 확인 원본은 보존됐지만 전체 대상·상세·순위가 아직 미확정.
- `detail_received`: 상세 원본은 받았지만 값 정리가 아직 끝나지 않음. 리뷰 원본도 받을 경우 함께 보존함.
- `provisional`: 해당 상품 처리는 끝났지만 전체 제외·순위 정리가 아직 미확정.
- `final`: 최종 CSV와 대조 가능한 확정값.
- `excluded`: 최종 대상에서 제외됨. CSV·DB 적재 대상에 포함하지 않음.

중단된 회차도 아래 명령으로 남은 기록을 확인할 수 있음. `--audit-only` 성공은 수집 완료를 뜻하지 않음.
`final_values_complete=false`이면 확정된 전체 결과로 검수하면 안 됨.

```powershell
python -m lowes.lowes_dryer_check --run-root "<run_root>" --audit-only
```

한 상품의 원본·최종값을 직접 보고 싶으면 실제 경로와 상품 ID로 바꿔 실행함.

```powershell
$dryerRunRoot = '<run_root>'
Get-Content -LiteralPath "$dryerRunRoot\audit\value_audit.jsonl" -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.item -eq '<상품_ID>' } |
    ConvertTo-Json -Depth 12
```

## CSV·누락 검수와 DB 비교

아래 `<run_root>`는 시작 로그에서 확인한 실제 경로로 바꿈.

```powershell
python -m lowes.lowes_dryer_check --run-root "<run_root>"
python -m lowes.step14_dryer_db_load --csv "<run_root>\output\final_output.csv" --verify-only
```

첫 명령은 외부 접속 없이 JSONL 확정값과 CSV의 전체 20개 컬럼을 대조하고,
CSV 컬럼·중복·순위·리뷰 수 일치·누락 사유도 검사함.
새 회차는 목록 CSV에서 제외·순위를 다시 계산하고, 제외 자료와 최종 상품이 겹치지 않는지도 검사함.
수정 전 회차도 기존 20개 컬럼 대조는 가능하며 `selection.verified=false`·`legacy_selection_policy`로 구분함. 미확정 JSONL은 정상 대조로 처리하지 않음.
둘째 명령은 DB를 읽어서 해당 배치의 가격·평점·리뷰 수·메타정보까지 CSV와 비교함.
DB 자동 생성 id는 NULL 여부·배치 내 중복을 따로 검사하며 나머지 19개 컬럼을 자료형에 맞춰 대조함.
자동 생성 규칙이 아닌 id는 CSV값과 직접 대조함. 가격의 소수·용량과 평점의 실수 저장 오차도 고려함.
불일치는 `differences`에 상품 ID·컬럼·예상값·실제값·사유를 표시하며 종료 코드는 1임.
10010에서 실제 사이트 접속·BSR 응답·전체 사양 누락·테이블 자료형은 원격 실행에서 확인해야 함.
테이블 컬럼 누락·자료형 변환 실패·필수 컬럼 NULL은 적재 전에 실패로 보고하며 조용히 누락시키지 않음.

DB 도구에서 확인할 때는 아래 `실제_batch_id`를 로그의 값으로 바꿔 실행함.

```sql
SELECT batch_id, count(*) AS rows,
       count(*) FILTER (WHERE main_rank IS NOT NULL) AS main_rows,
       count(*) FILTER (WHERE bsr_rank IS NOT NULL) AS bsr_rows,
       count(*) FILTER (WHERE main_rank IS NOT NULL AND bsr_rank IS NOT NULL) AS shared_rows,
       count(*) FILTER (WHERE loading_type IS NULL) AS loading_null,
       count(*) FILTER (WHERE capacity IS NULL) AS capacity_null
FROM public.ldy_dryer_retail_test
WHERE account_name = 'Lowes' AND batch_id = '실제_batch_id'
GROUP BY batch_id;

SELECT item, count(*)
FROM public.ldy_dryer_retail_test
WHERE account_name = 'Lowes' AND batch_id = '실제_batch_id'
GROUP BY item HAVING count(*) > 1;
```

## 재적재와 설정

CSV 검수가 끝난 같은 배치를 다시 적재할 때 사용함.
해당 테이블의 같은 `batch_id`·`Lowes` 행만 삭제·삽입하며 한 트랜잭션으로 처리함.
적재 실패는 롤백되며 다른 배치는 유지됨.
DB 이름(`database`)이 생략되거나 빈 값이면 기존 REF·LDY와 동일하게 `postgres`를 사용함.
명시된 DB 이름은 유지하며 `host`·`user`가 없으면 적재 전에 중단함.

```powershell
python -m lowes.step14_dryer_db_load --csv "<run_root>\output\final_output.csv"
```

단독 CSV 적재는 원래 수집 회차의 `manifest.json`을 갱신하지 않음.
재적재 성공 여부와 최종값 일치는 같은 CSV에 `--verify-only`를 붙여 별도로 확인함.

DB 연결 없이 수집·CSV만 확인하려면 다음 명령을 사용함.

```powershell
.\lowes\lowes_dryer_daily_task.bat --collect-only
```

환경 설정 이름: `LOWES_DRYER_MAIN_LIMIT`, `LOWES_DRYER_BSR_LIMIT`(1~100),
`LOWES_DRYER_MAX_MAIN_PAGES`, `LOWES_DRYER_MAX_ATTEMPTS`, `LOWES_DRYER_API_TIMEOUT`,
`LOWES_DRYER_RETRY_WAIT`, `LOWES_DRYER_PAGE_WAIT`, `LOWES_DRYER_SEED_WAIT`, `LOWES_DRYER_HEADLESS`,
`LOWES_DRYER_DETAIL_CONCURRENCY`(기본 2, 허용 1~2).
배치 이름을 지정하려면 `LOWES_DRYER_BATCH_ID`를 사용함. 기본 배치 형식은 기존 LDY의 `l_YYMMDD_HHMMSS`임.
지역 설정·DB 연결은 기존 런타임 로더로 전달하며 비밀 설정 파일 내용을 출력할 필요 없음.
건조기 기본 BAT은 테스트 테이블을 사용함. 본테이블 전환은 검수·별도 승인 후 진행함.

## 로컬 오프라인 검사

```powershell
python -m unittest -q lowes.test_dryer lowes.test_dryer_concurrency lowes.test_step00_uc
```

신규 검사는 가짜 API 응답과 가짜 DB 연결로 100/100 및 300/100 설정·제외·병합·사양·실패 유지·트랜잭션을 확인함.
환경 파일·실제 쇼핑몰·실제 DB는 열지 않음. 오프라인 통과가 원격 수집 성공을 뜻하지는 않음.
동시 처리 검사는 지연·역순 응답, 1/2/3/7개 및 홀수 개수, 최대 동시 수,
순차/동시 최종값·원본·순위 일치, 실패·재시도 격리, 대체 매장 전환, 중단 시 기록 보존을 포함함.
배송·픽업 검사는 건조기 필수 검사에 포함하지 않음. 기존 REF·LDY 검사 파일과 코드는 유지함.


## 가격·지역과 제외 자료 확인

가격 추출은 기존 목록 우선·상세 보완을 유지함. 장바구니 담기와 `pricingDataList` 보완은 실행하지 않음.
판매가만 있고 원가·할인이 없는 상품은 정상임.
원가·판매가가 있는데 할인액이 없으면 `missing_savings_with_both_prices`,
할인액이 있는데 판매가가 없으면 `missing_selling_with_savings`로 검수 경고를 남김.
이 경고는 `quality_warnings.json`, 상품별 JSONL `price_checks`, 오프라인 검수 출력에서 확인함.
값을 추측하거나 할인액을 계산해 채우지 않으며, 현재 사이트 가격을 과거 수집값의 정답으로 사용하지 않음.

지역 기록은 기존 응답만 사용하며 추가 배송·매장 API는 호출하지 않음.
`region.request.delivery_zip`은 요청한 10010이고 `region.response.store`는 응답 매장의 공개 정보임.
매장 ZIP이나 의미가 명확하지 않은 `location.zipcode`가 10010과 달라도 배송 ZIP 오류로 단정하지 않음.
명시된 배송 ZIP이 있으면 `delivery_zip_match` 또는 `delivery_zip_mismatch`로 기록함.
명시된 배송 ZIP이 없으면 `unverified`임. 배송 ZIP 일치만으로 가격 적용 지역까지 확정하지 않음.
`price_region_verified=false`는 가격 지역 미검증을 뜻하며 정상 적용이 확인됐다는 뜻이 아님.
실제 사이트 응답의 지역 의미와 값은 원격 재수집 후 검수해야 함.

제외 사유별 건수는 다음과 같이 확인함.

```powershell
$dryerRunRoot = '<run_root>'
Get-Content -LiteralPath "$dryerRunRoot\audit\excluded_items.jsonl" -Encoding UTF8 |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Group-Object reason | Select-Object Name, Count
python -m lowes.lowes_dryer_check --run-root "$dryerRunRoot"
```

`excluded_items.jsonl`에는 목록에서 명확하게 제외한 상품도 기록됨.
상품 ID가 없는 묶음 카드는 `item=null`이며 상품 URL·이름·사유로 추적함.
수집 중에도 제외 자료와 원본 JSONL을 저장하므로 중단 회차의 근거를 확인할 수 있음.
원래 목록에서 한도 밖이었던 상품은 `value_audit.jsonl`에서 `outside_final_targets`로 구분될 수 있음.
