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

기본값은 우편번호 **10010**, 메인 대상 최대 **100개**, Dryers BSR 표시 **1~100위**임.
메인은 Top Sellers(`sortBy_featured`), BSR은 `sortBy_bestSellers` API를 사용함.
기존 LDY의 매장 환경 설정을 재사용하고, 우편번호는 상속된 값과 관계없이 10010을 적용함.
기존 코드 기본 매장은 메인 1674, 상세 289임. `LOWES_API_STORE_ID`가 설정돼 있으면 양쪽에 적용함.
상세의 기존 대체 매장 설정도 재사용하고 실제 조회 매장은 사양 검수 자료에 기록함.

브라우저를 초기 접속한 뒤 같은 사이트 API로 목록·상세·리뷰 통계를 수집함.
상품 상세 페이지 이동, 리뷰 본문 여러 페이지, 추천·배송 API 수집은 필요 컬럼에 포함되지 않음.
로그에는 브라우저 버전·요청 지역·페이지·상세 진행률·오류·적재 결과가 표시됨.
기본 BAT은 `public.ldy_dryer_retail_test`에 적재함.
테이블 생성·변경, 다른 배치 삭제, 다른 크롤러 실행은 포함하지 않음.

메인의 확실한 단독 세탁기와 비대상 제품은 제외하고, 건조기·일체형의 광고는 포함함.
여러 SKU를 묶은 `/collections/` 카드는 기존 LDY처럼 개별 상품 대상에서 제외함.
종류가 애매하면 상세를 확인하며, 확인 실패는 검수 경고로 남김.
BSR은 해당 카테고리에 표시된 상품 전체를 포함함.
상품 ID 중복은 첫 노출을 유지하고 목록별 순위를 다시 부여함.
공통 상품은 한 번의 상세 수집 대상으로 묶고, 한 행에 두 순위를 저장함. 최종 행 수는 최대 200개임.
중복 때문에 BSR 고유 상품 수가 100개보다 적어도 101위를 가져와 채우지 않음.

최종 컬럼은 요청한 20개임. `id`는 실제 테이블의 자동 생성 규칙을 따름.
텍스트 가격은 기존 LDY처럼 `$499` 형식이며, DB가 숫자형이면 적재 전에 숫자로 변환함.
용량은 명시된 `Dryer Capacity`만 사용하며, 공용 `Washer Dryer Capacity`는 NULL임.
사양에 단위가 명시되면 `Cu.Feet`로 표기를 정리하고 원본·단위·저장값을 검수 자료에 남김.
`Dryer Load Type`의 명시된 투입 방식을 사용하며 전기·가스를 투입 방식으로 대체하지 않음.
가격은 목록 우선·상세 보완, 리뷰·평점은 상세 리뷰 통계 우선·목록 보완임.
평가 수는 리뷰 수와 같으며, 실제 0과 미수집 NULL을 구분함.
음수 가격·리뷰 수, 소수 리뷰 수, 0~5 범위를 벗어난 평점은 사용하지 않고 검수 경고로 남김.
정상 모델명을 다른 식별값으로 교체하지 않고, 목록·상세 모델 불일치는 경고로 남김.
상세에서 메인 전용 비대상 상품으로 확인되면 리뷰 호출은 생략함. BSR 상품은 계속 수집함.
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

예를 들어 목록 가격이 499, 상세 가격이 599이면 두 값을 보관하고,
`final_sku_price`의 출처가 목록이며 최종값이 `$499`임을 확인할 수 있음.
리뷰 수는 상세 통계 우선·목록 보완이며, 실제 0은 NULL 사유 없이 0으로 기록됨.
일체형 공용 용량은 원본값이 있어도 `shared_capacity_only` 사유와 최종 NULL을 기록함.
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
CSV 컬럼·중복·순위·리뷰 수 일치·누락 사유도 검사함. 미확정 JSONL은 정상 대조로 처리하지 않음.
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

```powershell
python -m lowes.step14_dryer_db_load --csv "<run_root>\output\final_output.csv"
```

DB 연결 없이 수집·CSV만 확인하려면 다음 명령을 사용함.

```powershell
.\lowes\lowes_dryer_daily_task.bat --collect-only
```

환경 설정 이름: `LOWES_DRYER_MAIN_LIMIT`, `LOWES_DRYER_BSR_LIMIT`(1~100),
`LOWES_DRYER_MAX_MAIN_PAGES`, `LOWES_DRYER_MAX_ATTEMPTS`, `LOWES_DRYER_API_TIMEOUT`,
`LOWES_DRYER_RETRY_WAIT`, `LOWES_DRYER_PAGE_WAIT`, `LOWES_DRYER_SEED_WAIT`, `LOWES_DRYER_HEADLESS`.
배치 이름을 지정하려면 `LOWES_DRYER_BATCH_ID`를 사용함. 기본 배치 형식은 기존 LDY의 `l_YYMMDD_HHMMSS`임.
지역 설정·DB 연결은 기존 런타임 로더로 전달하며 비밀 설정 파일 내용을 출력할 필요 없음.
건조기 기본 BAT은 테스트 테이블을 사용함. 본테이블 전환은 검수·별도 승인 후 진행함.

## 로컬 오프라인 검사

```powershell
python -m unittest -q lowes.test_dryer lowes.test_step00_uc
```

신규 검사는 가짜 API 응답과 가짜 DB 연결로 100/100 제한·병합·사양·실패 유지·트랜잭션을 확인함.
환경 파일·실제 쇼핑몰·실제 DB는 열지 않음. 오프라인 통과가 원격 수집 성공을 뜻하지는 않음.
배송·픽업 검사는 건조기 필수 검사에 포함하지 않음. 기존 REF·LDY 검사 파일과 코드는 유지함.
