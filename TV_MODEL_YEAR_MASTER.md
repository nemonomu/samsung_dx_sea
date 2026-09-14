# TV 모델 연도: 수동 마스터로 결측 보완

## 적용 규칙

대상은 Walmart TV 일반 수집·재수집과 Bestbuy 신규 파이프라인 TV의 `model_year`다.

| 마스터 연도 | 수집 연도 | 소매 테이블 적재 | 마스터 연도 |
|---|---|---|---|
| 2025 | 2026 | 2026 | 2025 유지 |
| 2025 | NULL 또는 빈값 | 2025 | 2025 유지 |
| NULL | NULL | NULL | NULL 유지 |
| NULL | 2026 | 2026 | NULL 유지 |

- 크롤러는 마스터의 `model_year`를 자동 등록·수정하지 않는다. 이 컬럼은 사람이 관리한다.
- 수집 연도가 있으면 수집값을 사용하며, 결측일 때만 마스터를 읽는다. 자동 재사용 가능한 연도는 4자리 숫자다.
- 조회 범위는 같은 `account_name + item`이고 `is_product = TRUE`인 마스터다.
- 중복 행은 `id DESC` 순서에서 유효한 연도가 처음 나오는 행을 사용한다. 최신 행이 NULL이면 이전 유효 행으로 보완한다.
- 자동 증가 id를 전제로 최신 행을 판단하며 `updated_at`이나 연도의 크기는 기준으로 사용하지 않는다.
- 과거 소매 이력에만 수동 입력한 값은 가져오지 않는다. 과거 행 전체를 소급 수정하지도 않는다.
- SKU·화면 크기 등 기존 마스터 처리와 Amazon·REF·LDY 코드는 이번 정책 변경 대상이 아니다. 마스터 테이블 전체가 읽기 전용이라는 의미는 아니다.

## 수동 입력 방법

같은 쇼핑몰·상품의 최신 활성 마스터 행에 모델 연도를 입력하고 DB 편집기에서 저장·커밋한다. 예를 들어 `2025`처럼 4자리로 입력한다. 수집이 시작되기 전에 커밋하면 다음 조회에 반영된다.

```sql
SELECT id, account_name, item, model_year, is_product
FROM public.tv_item_mst
WHERE account_name = 'Walmart'
  AND item = '<상품 item>'
ORDER BY id DESC;
```

Bestbuy는 `account_name = 'Bestbuy'`로 조회한다. 모델 연도는 개별 제품의 실제 제조일과 다르다.

## 코드 동작

### Walmart

- `wmart_tv_dt.py`는 소매 저장 직전에 결측 연도를 마스터로 보완한다.
- `wmart_tv_dt_update.py`의 재수집 UPDATE도 같은 보완 함수를 사용한다.
- 재수집에서 수집값과 마스터값이 모두 없으면 기존 소매 행의 연도를 보존한다. 새 INSERT는 NULL이다.
- 기존 마스터 등록·SKU·화면 크기 보완은 유지하지만, `model_year`는 INSERT/UPDATE SQL에서 제외한다.
- 테스트 모드와 소매 저장 실패 경로도 마스터 연도를 바꾸지 않는다. 다른 마스터 필드의 기존 저장·커밋 방식은 유지한다.
- 상세 수집 실패 시 목록 정보만 저장하는 경로도 같은 상품이면 기존 마스터 연도로 보완한다.

### Bestbuy

- `step14_db_load.py`는 TV 최종 적재 대상 행의 결측 연도를 보완한다. 원본 `final_output.csv`는 바꾸지 않는다.
- `step15_item_mst_load.py`는 마스터 출력 CSV와 INSERT/UPDATE 대상에서 `model_year`를 제외한다. 과거 CSV에 연도가 있어도 마스터 연도에 쓰지 않는다.
- 수동 입력용 스키마 컬럼은 유지한다. 마스터 테이블·필수 컬럼이 없으면 `master_table_or_columns_missing`을 기록하고 보완을 건너뛴다.
- 마스터가 준비되어 있다면, 기존 수동 연도 보완을 위해 실행 단계 16번 `item_mst_load`의 완료를 기다릴 필요가 없다.
- `db_load_manifest.json`의 `model_year_master`에서 보완 건수와 `selection: newest_id`를 확인할 수 있다.
- 상품 목록·다른 카테고리·프로모션/재고/유사상품 전용 복구 모드에는 연도 보완을 적용하지 않는다. `dry_run`은 보완 조회도 실행하지 않는다.

마스터 연도 조회는 행 잠금을 추가하지 않는 일반 SELECT다. 수동 입력과 조회가 동시에 일어나면 조회 시점에 커밋되어 보이는 값을 사용한다.

## 검증

```powershell
python -B -m unittest test_tv_model_year_master -v
```

실제 함수·클래스의 코드를 불러와 가짜 커서와 메모리 SQLite에서 수집값 우선, 수동값 보완, 연도 쓰기 차단, 중복·다른 상품 분리, 일반·재수집·테스트 모드, 저장 실패, 과거 데이터 재처리를 검증한다.

운영 DB·실사이트에 연결하지 않는 검사다. 운영 PostgreSQL 권한·컬럼 형식·DB 트리거와 동시 실행은 별도 확인 대상이다. `model_year` 외 기존 마스터 저장 문제까지 해결하는 변경은 아니다.
