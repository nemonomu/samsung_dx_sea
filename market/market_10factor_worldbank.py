"""
World Bank API v2 데이터 수집기

================================================================================
API 정보
================================================================================
Base URL: https://api.worldbank.org/v2
문서: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392

지표 코드 검색: https://data.worldbank.org/indicator

================================================================================
지표 목록
================================================================================
- NY.GDP.PCAP.PP.CD: GDP PPP 명목 1인당
- NY.GDP.PCAP.PP.KD: GDP PPP 실질 1인당
- NY.GNP.PCAP.PP.CD: GNI PPP 1인당 (가처분소득 대리)
- FP.CPI.TOTL: CPI
- NY.GNP.PCAP.CN: GNI 명목 LCU
- FS.AST.DOMS.GD.ZS: 국내신용 (% of GDP)

================================================================================
"""

import os
import sys
import logging
import traceback
import requests
import time
import msvcrt
import psycopg2
import re
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_CONFIG

# ============================================================================
# 로깅 설정
# ============================================================================

logger = None


def setup_logger(log_file=None):
    """로거 설정"""
    global logger
    logger = logging.getLogger('market_worldbank')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, log_file)

        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        return log_path

    return None


def print_log(level, message):
    """로그 출력"""
    if logger:
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(message)
    else:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{timestamp}] [{level}] {message}")

# ============================================================================
# 설정
# ============================================================================

BASE_URL = "https://api.worldbank.org/v2"

INDICATORS = {
    'gdp_ppp_nominal': {
        'code': 'NY.GDP.PCAP.PP.CD',
        'name': 'GDP PPP 명목 1인당',
        'unit': 'USD (PPP)',
        'default_period': 'last_year',
        'table': 'market_gdp_ppp_nominal',
        'test_table': 'test_market_gdp_ppp_nominal'
    },
    'gdp_ppp_real': {
        'code': 'NY.GDP.PCAP.PP.KD',
        'name': 'GDP PPP 실질 1인당',
        'unit': 'USD (constant {year} PPP)',
        'default_period': 'last_year',
        'table': 'market_gdp_ppp_real',
        'test_table': 'test_market_gdp_ppp_real'
    },
    'disposable_income_real': {
        'code': 'NY.GNP.PCAP.PP.CD',
        'name': '가처분소득 (실질 PPP)',
        'unit': 'USD (PPP)',
        'default_period': 'last_year',
        'table': 'market_disposable_income_real',
        'test_table': 'test_market_disposable_income_real'
    },
    'cpi': {
        'code': 'FP.CPI.TOTL',
        'name': 'CPI',
        'unit': 'Index (2010=100)',
        'default_period': 'last_year',
        'table': 'market_cpi',
        'test_table': 'test_market_cpi'
    },
    'disposable_income_nominal': {
        'code': 'NY.GNP.PCAP.CN',
        'name': '가처분소득 (명목 LCU)',
        'unit': 'LCU per capita',
        'default_period': 'last_year',
        'table': 'market_disposable_income_nominal',
        'test_table': 'test_market_disposable_income_nominal'
    },
    'household_debt': {
        'code': 'FS.AST.DOMS.GD.ZS',
        'name': '가계부채',
        'unit': '% of GDP',
        'default_period': 'last_year',
        'table': 'market_household_debt',
        'test_table': 'test_market_household_debt'
    }
}

MAX_RETRIES = 3
RETRY_DELAY = 1
REQUEST_TIMEOUT = 30


# ============================================================================
# HTTP 요청
# ============================================================================

def make_request(url, timeout=REQUEST_TIMEOUT):
    """HTTP 요청 (재시도 포함)"""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"[ERROR] 요청 실패: {e}")
                return None
            print(f"[WARNING] 재시도 {attempt + 1}/{MAX_RETRIES}...")
            time.sleep(RETRY_DELAY)
    return None


# ============================================================================
# DB 저장
# ============================================================================

def save_api_request(api_name, batch_id, request_url, response_json):
    """API 요청 로그 저장"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO market_10factor_api_request
                (api_name, batch_id, request_url, response_json, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            api_name,
            batch_id,
            request_url,
            response_json,
            datetime.now()
        ))

        conn.commit()
        cursor.close()
        conn.close()
        print_log("INFO", f"API 요청 로그 저장 완료")
        return True
    except Exception as e:
        print_log("ERROR", f"API 요청 로그 저장 실패: {e}")
        return False


def save_to_db(results, batch_id, table_name='market_worldbank'):
    """DB 저장 (period + country_code + indicator 중복 시 skip)"""
    if not results:
        print_log("WARNING", "저장할 데이터 없음")
        return False

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        created_at = datetime.now()

        inserted = 0
        skipped = 0

        for row in results:
            # 중복 체크 (NY.GDP.PCAP.PP.KD인 경우 unit도 포함)
            indicator_key = row.get('indicator_key', '')
            if indicator_key == 'NY.GDP.PCAP.PP.KD':
                # gdp_ppp_real: period + country_code + indicator + unit 중복 체크
                cursor.execute(f"""
                    SELECT 1 FROM {table_name}
                    WHERE period = %s AND country_code = %s AND indicator = %s AND unit = %s
                """, (row['period'], row['country_code'], indicator_key, row['unit']))
            else:
                # 기타: period + country_code + indicator 중복 체크
                cursor.execute(f"""
                    SELECT 1 FROM {table_name}
                    WHERE period = %s AND country_code = %s AND indicator = %s
                """, (row['period'], row['country_code'], indicator_key))

            if cursor.fetchone():
                skipped += 1
                continue

            # INSERT
            cursor.execute(f"""
                INSERT INTO {table_name}
                    (period, country_code, indicator, value, unit, source, batch_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                row['period'],
                row['country_code'],
                row.get('indicator_key', ''),
                row['value'],
                row['unit'],
                row['source'],
                batch_id,
                created_at
            ))
            inserted += 1

        conn.commit()
        cursor.close()
        conn.close()

        print_log("INFO", f"DB 저장 완료 ({table_name}): INSERT {inserted}건, SKIP {skipped}건")
        return True

    except Exception as e:
        print_log("ERROR", f"DB 저장 실패: {e}")
        traceback.print_exc()
        return False


# ============================================================================
# 유틸리티 함수
# ============================================================================

def input_with_timeout(prompt, timeout=10):
    """타임아웃 지원 입력 (Windows)"""
    print(f"{prompt}: ", end='', flush=True)

    value = ''
    start_time = time.time()
    while time.time() - start_time < timeout:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == '\r':
                print()
                break
            elif char == '\b':
                if value:
                    value = value[:-1]
                    print('\b \b', end='', flush=True)
            else:
                value += char
                print(char, end='', flush=True)
        time.sleep(0.1)
    else:
        print("\n시간 초과")
        return None

    return value.strip() if value.strip() else None


# ============================================================================
# 모드 설정
# ============================================================================

MODE_CONFIG = {
    'dry': {
        'name': 'DRY RUN',
        'batch_prefix': 'dry_',
        'save_log': False
    },
    'test': {
        'name': 'TEST MODE',
        'batch_prefix': 't_',
        'save_log': True
    },
    'prod': {
        'name': '운영 모드',
        'batch_prefix': '',
        'save_log': True
    }
}


# ============================================================================
# 데이터 수집
# ============================================================================

def collect_data_with_period(indicator_code, unit, source_name="World Bank", countries=None, start_year=None, end_year=None):
    """기간 지정 데이터 수집 (복수 국가 한 번에 호출)

    Returns:
        tuple: (data_rows, request_url, response_json)
    """
    data_rows = []
    request_url = None
    response_json = None

    period_desc = "전체" if start_year is None else f"{start_year}~{end_year}"
    print_log("INFO", f"[World Bank] {indicator_code} 수집 ({period_desc})")

    # countries가 None이면 전체 국가(all), 리스트면 세미콜론으로 연결
    if countries is None:
        countries_str = "all"
        print_log("INFO", "  대상 국가: 전체")
    else:
        countries_str = ";".join(countries)
        print_log("INFO", f"  대상 국가: {', '.join(countries)}")

    # URL 생성 (start_year가 None이면 기간 파라미터 없이)
    if start_year is None:
        request_url = f"{BASE_URL}/country/{countries_str}/indicator/{indicator_code}?format=json&per_page=10000"
    else:
        request_url = f"{BASE_URL}/country/{countries_str}/indicator/{indicator_code}?format=json&date={start_year}:{end_year}&per_page=10000"

    print_log("INFO", f"  요청 URL: {request_url}")

    response = make_request(request_url, timeout=60)
    if not response:
        print_log("ERROR", "API 요청 실패")
        return data_rows, request_url, response_json

    try:
        json_data = response.json()
        response_json = response.text

        if len(json_data) < 2 or not json_data[1]:
            print_log("WARNING", "데이터 없음")
            return data_rows, request_url, response_json

        # gdp_ppp_real (NY.GDP.PCAP.PP.KD)인 경우 API 응답에서 연도 추출 후 unit 포맷팅
        if indicator_code == 'NY.GDP.PCAP.PP.KD' and json_data[1]:
            first_data = json_data[1][0]
            indicator_name = first_data.get('indicator', {}).get('value', '')
            # 괄호 안에서 연도 추출: "GDP per capita, PPP (constant 2021 international $)" → "2021"
            year_match = re.search(r'constant (\d{4})', indicator_name)
            if year_match:
                base_year = year_match.group(1)
                api_unit = f'USD (constant {base_year} PPP)'
                print_log("INFO", f"  API에서 추출한 기준연도: {base_year} → unit: {api_unit}")
            else:
                api_unit = unit.replace('{year}', '')
                print_log("WARNING", f"  기준연도 추출 실패, 기본 unit 사용: {api_unit}")
        else:
            api_unit = unit

        for data_point in json_data[1]:
            if data_point['value'] is not None:
                country_code = data_point.get('countryiso3code', '')
                # countryiso3code가 빈 문자열이면 건너뛰기 (소득 그룹 등 제외)
                if not country_code:
                    continue
                country_name = data_point['country']['value']

                data_rows.append({
                    'period': int(data_point['date']),
                    'country_code': country_code,
                    'country_name': country_name,
                    'value': float(data_point['value']),
                    'unit': api_unit,
                    'source': source_name
                })

    except (KeyError, ValueError, TypeError) as e:
        print_log("ERROR", f"파싱 오류: {e}")
        traceback.print_exc()

    print_log("INFO", f"[World Bank] 수집 완료: 총 {len(data_rows)}건")
    return data_rows, request_url, response_json


# ============================================================================
# 메인 실행
# ============================================================================

def run(mode='prod', indicators=None):
    """
    통합 실행 함수

    Args:
        mode: 실행 모드 ('dry', 'test', 'prod')
        indicators: 수집할 지표 리스트 (None이면 직접 실행 - 프롬프트 표시)
    """
    # 외부 호출 여부 판단: indicators가 있으면 외부 호출
    is_external_call = indicators is not None

    config = MODE_CONFIG.get(mode, MODE_CONFIG['prod'])
    current_year = datetime.now().year
    indicator_list = list(INDICATORS.keys())

    # ========================================
    # 파라미터 결정 (로거 설정 전에 지표 선택)
    # ========================================
    if is_external_call:
        # 외부 호출: 단일 지표, 전체 국가, 지표별 기본 기간
        indicator_key = indicators
        indicator = INDICATORS[indicator_key]
        selected_countries = None
        default_period = indicator.get('default_period', 'last_year')

        if default_period == 'last_year':
            start_year = current_year - 1
            end_year = current_year - 1
        else:
            start_year = None
            end_year = None

        print(f"[지표] {indicator_key}")
        print("[국가] 전체")
        print(f"[기간] {'전체' if start_year is None else f'{start_year}~{end_year}'}")

    else:
        # 직접 실행: 프롬프트로 선택
        print("[수집 가능 지표]")
        for i, key in enumerate(indicator_list, 1):
            info = INDICATORS[key]
            print(f"  {i}. {key}: {info['name']} ({info['code']})")
        print()

        print("[지표 선택] 번호 입력")
        indicator_input = input("지표 입력: ").strip()

        if not indicator_input.isdigit():
            print_log("ERROR", "번호를 입력해주세요")
            return []

        idx = int(indicator_input) - 1
        if idx < 0 or idx >= len(indicator_list):
            print_log("ERROR", "잘못된 번호입니다")
            return []

        indicator_key = indicator_list[idx]
        indicator = INDICATORS[indicator_key]
        print(f"선택된 지표: {indicator_key}")

        # 국가 선택
        print("\n[국가 선택] (10초 후 전체 국가 수집)")
        print("  all 또는 엔터: 전체 국가")
        print("  USA,KOR,JPN: 특정 국가 (ISO 3자리 코드)")

        country_input = input_with_timeout("국가 입력", timeout=10)

        if not country_input or country_input.lower() == 'all':
            selected_countries = None
            print("선택된 국가: 전체")
        else:
            selected_countries = [code.strip() for code in country_input.upper().split(',') if code.strip()]
            print(f"선택된 국가: {', '.join(selected_countries)}")

        # 기간 선택
        print(f"\n[기간 선택] (10초 후 작년 데이터 수집)")
        print(f"  2015: 특정 연도")
        print("  2010-2015: 범위 지정")
        print(f"  all: 전체 기간")
        print(f"  엔터: 작년 ({current_year - 1})")

        period_input = input_with_timeout("기간 입력", timeout=10)

        if period_input and period_input.lower() == 'all':
            start_year = None
            end_year = None
        elif period_input and '-' in period_input:
            parts = period_input.split('-')
            start_year = int(parts[0].strip())
            end_year = int(parts[1].strip())
        elif period_input and period_input.isdigit():
            start_year = int(period_input)
            end_year = int(period_input)
        else:
            start_year = current_year - 1
            end_year = current_year - 1

        if start_year is None:
            print("선택된 기간: 전체")
        else:
            print(f"선택된 기간: {start_year} ~ {end_year}")

    # ========================================
    # 로거 설정 (지표 선택 후)
    # ========================================
    batch_id = config['batch_prefix'] + datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = None
    if config['save_log']:
        log_file = f"worldbank_{indicator_key}_{batch_id}.log"
        log_path = setup_logger(log_file)
    else:
        setup_logger()

    # 헤더 출력
    print_log("INFO", "=" * 60)
    print_log("INFO", f"Market 10대 인자 - World Bank [{config['name']}]")
    print_log("INFO", "=" * 60)
    print_log("INFO", f"배치 ID: {batch_id}")
    print_log("INFO", f"지표: {indicator_key} ({indicator['code']})")
    if log_path:
        print_log("INFO", f"로그 파일: {log_path}")

    # ========================================
    # 데이터 수집 (공통)
    # ========================================
    print_log("INFO", "")
    print_log("INFO", "데이터 수집 시작")
    print_log("INFO", "-" * 50)

    period_desc = "전체" if start_year is None else f"{start_year}~{end_year}"
    print_log("INFO", f"{indicator_key} 수집 중... ({period_desc})")

    data, request_url, response_json = collect_data_with_period(
        indicator_code=indicator['code'],
        unit=indicator['unit'],
        source_name="World Bank",
        countries=selected_countries,
        start_year=start_year,
        end_year=end_year
    )

    for row in data:
        row['indicator_key'] = indicator['code']

    print_log("INFO", f"{indicator_key}: {len(data)}건 수집 완료")

    # 수집 데이터 출력
    if data:
        print_log("INFO", f"조회 결과: {len(data)}건")
        print_log("INFO", "-" * 80)
        print_log("INFO", f"  {'No':<8} {'Country':<10} {'Indicator':<20} {'Period':<6} {'Value':<15} {'Unit'}")
        print_log("INFO", "-" * 80)
        sorted_data = sorted(data, key=lambda x: (x['period'], x['country_code']))
        for i, row in enumerate(sorted_data, 1):
            print_log("INFO", f"  {i:<8} {row['country_code']:<10} {indicator['code']:<20} {row['period']:<6} {row['value']:>25,} {row['unit']}")
        print_log("INFO", "-" * 80)

    # ========================================
    # DB 저장 (공통)
    # ========================================
    if mode == 'dry':
        table_name = None
    elif mode == 'test':
        table_name = indicator.get('test_table')
    else:
        table_name = indicator.get('table')

    if table_name and data:
        print("\n" + "-" * 60)
        save_api_request(f'worldbank_{indicator_key}', batch_id, request_url, response_json)
        save_to_db(data, batch_id, table_name=table_name)

    # 완료 메시지
    print("\n" + "=" * 60)
    if table_name:
        print_log("INFO", f"[{config['name']}] 완료 - {len(data)}건 저장 ({table_name})")
    else:
        print_log("INFO", f"[{config['name']}] 완료 - DB 저장 없음 (DRY RUN)")
    print("=" * 60)

    return data


if __name__ == "__main__":
    print("\n[모드 선택] (10초 후 자동으로 운영모드 실행)")
    print("  d: DRY RUN (API 응답 확인)")
    print("  t: TEST MODE (test_market_worldbank)")
    print("  엔터: 운영 모드 (market_worldbank)")
    print()

    mode = ''
    print("모드 입력: ", end='', flush=True)
    start_time = time.time()
    while time.time() - start_time < 10:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == '\r':
                print()
                break
            mode = char.lower()
            print(char)
            break
        time.sleep(0.1)
    else:
        print("\n시간 초과 - 운영 모드 자동 실행")

    try:
        if mode == 'd':
            run('dry')
        elif mode == 't':
            run('test')
        else:
            run('prod')

        input("\n엔터키를 누르면 종료합니다...")
    except Exception as e:
        print(f"\n[ERROR] 예외 발생: {e}")
        traceback.print_exc()
