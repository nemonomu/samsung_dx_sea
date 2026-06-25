# HHP Retail Crawler - 개발 가이드

## 프로젝트 구조

### 디렉토리 레이아웃

```
samsung_dx_hhp_retail_com/
├── common/                          # 공통 모듈 (전체 공유)
│   ├── setup.py                     # 환경 설정 (경로, UTF-8, sys.path)
│   ├── base_crawler.py              # BaseCrawler 기본 클래스
│   ├── data_extractor.py            # 텍스트/숫자 추출 유틸리티
│   └── alert_hhp_monitor.py         # 크롤링 완료 이메일 알림
├── config/                          # DB/API 설정
├── market/                          # 시장 분석 (경쟁사, 트렌드)
├── retail_sentiment/                # 리테일 감성 분석
│
│── hhp/                             # [기존] 상품명 기준 구조 (유지)
│   ├── amazon/
│   ├── bestbuy/
│   └── walmart/
├── tv/                              # [기존] TV 크롤러 (유지)
│
└── {법인명}/                         # [신규] 법인별 구조
    └── {상품명}/
        └── {리테일러명}/
            ├── {법인명}_{retailer}_{product}_crawl.py       # 오케스트레이터 (프로덕션)
            ├── {법인명}_{retailer}_{product}_crawl_test.py   # 오케스트레이터 (테스트)
            ├── {법인명}_{retailer}_{product}_main.py          # 워커: 제품 리스트 수집
            ├── {법인명}_{retailer}_{product}_bsr.py           # 워커: BSR 수집
            ├── {법인명}_{retailer}_{product}_dt.py            # 워커: 상세 정보 수집
            └── {법인명}_{retailer}_{product}_item.py          # 워커: SKU 추출
```

### 신규 법인별 구조 예시

```
seal/
├── hhp/
│   ├── amazon/
│   │   ├── seal_amazon_hhp_crawl.py
│   │   ├── seal_amazon_hhp_crawl_test.py
│   │   ├── seal_amazon_hhp_main.py
│   │   ├── seal_amazon_hhp_bsr.py
│   │   ├── seal_amazon_hhp_dt.py
│   │   └── seal_amazon_hhp_item.py
│   └── bestbuy/
│       ├── seal_bestbuy_hhp_crawl.py
│       └── ...
└── tv/
    └── amazon/
        ├── seal_amazon_tv_crawl.py
        └── ...
```

### 구조 규칙

- **기존 폴더**(hhp/, tv/)는 그대로 유지, 건드리지 않음
- **신규 개발**은 반드시 `{법인명}/{상품명}/{리테일러명}/` 구조로 생성
- **common/, market/, retail_sentiment/**는 프로젝트 루트에 유지 (법인 공통)
- 모든 크롤러는 `common/` 모듈을 import하여 사용

## DB 테이블 구조

### 통합 테이블 (법인 공통) — 모든 법인이 동일 테이블 조회

아래 테이블은 **법인 구분 없이 단일 테이블**로 운영한다. 법인별 분기는 테이블이 아니라 `corp` 컬럼으로 구분한다. 신규 법인이 추가되어도 별도 테이블을 만들지 않는다.

| 용도 | 테이블명 | 식별 컬럼 |
|---|---|---|
| XPath | `dx_xpath_selectors` | `corp`, `product_line`, `account_name`, `page_type`, `data_field` |
| 타겟 URL | `dx_target_page_url` | `corp`, `product_line`, `account_name`, `page_type` |

조회 시 반드시 `corp`, `product_line` 조건을 함께 명시할 것 (예: HHP 크롤러는 `corp='SEA' AND product_line='HHP'`, TV 크롤러는 `corp='SEA' AND product_line='TV'`).

### 기존 테이블 (참고용)

| 용도 | 테이블명 | 설명 |
|---|---|---|
| 제품 리스트 | `amazon_hhp_product_list` | Main/BSR 단계 수집 결과 |
| 저장 테이블 | `hhp_retail_com` | 프로덕션 상세 데이터 |
| 테스트 테이블 | `test_hhp_retail_com` | 테스트 상세 데이터 |
| 아이템 마스터 | `hhp_item_mst` | SKU/모델번호 마스터 |

### 신규 테이블 네이밍 규칙

신규 개발 시 기존 테이블을 사용하지 않고, `{법인명}_` 프리픽스를 붙여 새로 생성한다.
**단, 아래 테이블은 컬럼 구조(컬럼명, 타입, 순서)를 기존 테이블과 반드시 동일하게 생성할 것.** 향후 법인별 테이블을 하나로 통합할 예정이므로 스키마가 달라지면 안 된다.
- `item_mst` (아이템 마스터)

> XPath / 타겟 URL 테이블은 이미 `dx_xpath_selectors` / `dx_target_page_url`로 통합되어 법인별 생성 대상에서 제외된다.

| 용도 | 네이밍 패턴 | 예시 (seal 법인) |
|---|---|---|
| 제품 리스트 | `{법인명}_{retailer}_{product}_product_list` | `seal_amazon_hhp_product_list` |
| 저장 테이블 | `{법인명}_{product}_retail_com` | `seal_hhp_retail_com` |
| 테스트 테이블 | `test_{법인명}_{product}_retail_com` | `test_seal_hhp_retail_com` |
| 아이템 마스터 | `{법인명}_{product}_item_mst` | `seal_hhp_item_mst` |

## 크롤러 실행 흐름

### 오케스트레이터 → 워커 4단계 파이프라인

```
amazon_hhp_crawl.py (오케스트레이터)
  │
  ├─ STAGE 1: Main   → 검색 결과 제품 수집     → amazon_hhp_product_list
  ├─ STAGE 2: BSR    → 베스트셀러 랭크 수집     → amazon_hhp_product_list (bsr_rank 업데이트)
  ├─ STAGE 3: Detail → 제품 상세 정보 수집      → hhp_retail_com
  └─ STAGE 4: Item   → SKU/모델번호 추출        → hhp_item_mst
```

- 각 단계는 try/except로 래핑되어 한 단계 실패해도 파이프라인 계속 진행
- 완료 후 `send_crawl_alert()`로 이메일 알림 발송
- `--resume-from {main|bsr|detail|item} --batch-id {id}` 로 특정 단계부터 재개 가능

### 실행 모드

| 실행 방법 | 파일 | batch_id | 저장 테이블 |
|---|---|---|---|
| 프로덕션 | `amazon_hhp_crawl.py` | `a_YYYYMMDD_HHMMSS` | `hhp_retail_com` |
| 테스트 (오케스트레이터) | `amazon_hhp_crawl_test.py` | `t_a_YYYYMMDD_HHMMSS` | `test_hhp_retail_com` |
| 테스트 (워커 개별 실행) | `amazon_hhp_main.py` 등 | `t_a_YYYYMMDD_HHMMSS` | `test_hhp_retail_com` |

### batch_id 규칙

- 형식: `{prefix}_{YYYYMMDD}_{HHMMSS}`
- 프리픽스: `a_` (Amazon), `b_` (BestBuy), `w_` (Walmart)
- 테스트 모드: `t_` 프리픽스 추가 → `t_a_YYYYMMDD_HHMMSS`
- `generate_batch_id(account_name, test_mode)` 메서드로 생성

### dt(Detail) 개별 실행 시 주의사항

- dt 파일은 개별 실행 시 테스트 모드로 동작
- 파일 내에 실행할 batch_id를 직접 설정해야 함 (346라인 부근)
- test_hhp_retail_com 테이블에 저장됨

## common 모듈 상세

### setup.py

모든 크롤러 파일 최상단에서 호출:
```python
from common.setup import setup_environment
setup_environment(__file__)
```
- 작업 디렉토리를 프로젝트 루트로 설정
- Windows UTF-8 콘솔 출력 활성화
- 프로젝트 루트를 sys.path에 추가

### base_crawler.py — BaseCrawler 클래스

모든 워커 크롤러의 부모 클래스. 아래 기능 제공:

**DB 연결/조회:**
- `connect_db()` — psycopg2 연결 (config.py의 DB_CONFIG 사용)
- `load_xpaths(account_name, page_type, corp, product_line)` — `dx_xpath_selectors` 테이블에서 XPath 로드. 호출 시 `corp`/`product_line` 인자 필수 (예: HHP는 `'SEA', 'HHP'`, TV는 `'SEA', 'TV'`)
- `load_page_urls(account_name, page_type, corp, product_line)` — `dx_target_page_url` 테이블에서 URL 템플릿 로드. `corp`/`product_line` 인자 필수
- `execute_insert()` / `execute_update()` — DB 쓰기

**배치/로깅:**
- `generate_batch_id(account_name, test_mode)` — batch_id 생성
- `generate_calendar_week()` — ISO 주차 (w01~w52)
- `start_logging(batch_id)` / `stop_logging()` — 파일 로깅

**WebDriver:**
- `setup_driver()` — 표준 Chrome WebDriver (Selenium)
- `setup_driver_stealth()` — 봇 탐지 우회 강화 버전 (Amazon용)

**쿠키 관리:**
- `save_cookies(account_name)` — pickle 저장
- `load_cookies(account_name, suffix)` — pickle 로드 (Amazon은 suffix 1/2/3 지원)

**유틸리티:**
- `check_product_exists()` — 중복 체크
- `update_product_rank()` — 랭크 업데이트
- `cleanup_old_logs()` — 30일 이상 로그 삭제
- `retry_on_network_error()` — 네트워크 에러 재시도 데코레이터

### data_extractor.py

- `extract_numeric_value(text)` — 텍스트에서 숫자 추출
- `get_no_reviews_text(account_name)` — 리테일러별 "리뷰 없음" 텍스트
- `extract_text_before_or_after(raw_text, cut_text, position)` — 텍스트 분리

### alert_hhp_monitor.py

- `send_crawl_alert(retailer, results, failed_stages, elapsed_time, ...)` — 크롤링 결과 이메일 발송
- 오케스트레이터의 run() 완료 시 호출
- 테스트 모드일 경우 제목에 `[TEST]` 프리픽스

## 새 리테일러 개발 시 따라야 할 패턴

**모든 리테일러는 Amazon과 동일한 구조를 따른다.**

### 1. 파일 구성

신규 개발 시 법인별 구조를 따른다:
```
{법인명}/{상품명}/{리테일러명}/
├── {법인명}_{retailer}_{product}_crawl.py          # 오케스트레이터 (프로덕션)
├── {법인명}_{retailer}_{product}_crawl_test.py     # 오케스트레이터 (테스트)
├── {법인명}_{retailer}_{product}_main.py           # 워커: 제품 리스트 수집
├── {법인명}_{retailer}_{product}_bsr.py            # 워커: BSR 수집
└── {법인명}_{retailer}_{product}_dt.py             # 워커: 상세 정보 수집
```

### 2. 워커 작성 규칙

```python
# 파일 최상단 — 반드시 setup 호출
import sys, os
from common.setup import setup_environment
setup_environment(__file__)

from common.base_crawler import BaseCrawler

class {Retailer}{Stage}Crawler(BaseCrawler):
    def __init__(self, test_mode=True, batch_id=None):
        super().__init__()
        self.test_mode = test_mode
        self.batch_id = batch_id
        self.account_name = '{Retailer}'
        self.page_type = '{stage}'

    def initialize(self):
        # DB 연결 → XPath 로드 → WebDriver 설정 → batch_id 생성

    def run(self):
        # 메인 실행 로직
```

### 3. 오케스트레이터 작성 규칙

```python
class {Retailer}IntegratedCrawler:
    def run(self):
        batch_id = self.generate_batch_id('{retailer}', test_mode=False)
        # Stage 1~4 순차 실행, 각 단계 try/except 래핑
        # 완료 후 send_crawl_alert() 호출
```

### 4. 테스트 오케스트레이터

- 프로덕션과 동일 흐름
- `test_mode=True` 전달 → batch_id에 `t_` 프리픽스
- 수집 건수 제한 (test_count 사용)
- `test_hhp_retail_com` 테이블에 저장

### 5. 레퍼런스 파일 (복사해서 시작)

| 역할 | 원본 경로 |
|---|---|
| 오케스트레이터 (프로덕션) | `hhp/amazon/amazon_hhp_crawl.py` |
| 오케스트레이터 (테스트) | `hhp/amazon/amazon_hhp_crawl_test.py` |
| 워커: Main | `hhp/amazon/amazon_hhp_main.py` |
| 워커: BSR | `hhp/amazon/amazon_hhp_bsr.py` |
| 워커: Detail | `hhp/amazon/amazon_hhp_dt.py` |

오케스트레이터 구조, 에러 핸들링, 이메일 알림, 로깅 패턴 모두 동일하게 유지.

### 주의사항

- 신규 생성되는 크롤러는 **리테일러 종류와 관계없이 모두 로그인 없이** 동작하도록 개발한다. (Amazon, BestBuy, Walmart 등 전부 해당)
- 기존 Amazon 레퍼런스의 쿠키/로그인 관련 로직(`load_cookies`, `save_cookies`, `run_login_and_reload_cookies`, 쿠키 suffix 로테이션 등)은 복사하지 않는다.

### 6. 실행 명령어

```bash
# 프로덕션 실행
python seal/hhp/amazon/seal_amazon_hhp_crawl.py

# 테스트 실행
python seal/hhp/amazon/seal_amazon_hhp_crawl_test.py

# 워커 개별 실행 (테스트 모드)
python seal/hhp/amazon/seal_amazon_hhp_main.py

# 특정 단계부터 재개
python seal/hhp/amazon/seal_amazon_hhp_crawl.py --resume-from detail --batch-id a_20260413_143045
```
