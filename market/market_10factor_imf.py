"""
IMF SDMX 3.0 REST API 데이터 수집기

================================================================================
API 정보
================================================================================
Base URL: https://api.imf.org/external/sdmx/3.0
문서: https://portal.api.imf.org/apis#tags=iData
    - https://data.imf.org/Datasets/ICSD
    - https://data.imf.org/en/datasets/IMF.STA:FSIC



================================================================================
지표 목록
================================================================================
1. CAPSTCK_PS_V_XDC: 자본스톡 (민간, 현재가격) - ICSD 데이터셋
2-1. NINTINC_XDC(Net interest income): 순 이자수입 (Domestic Currency) - FSIBSIS 데이터셋
2-2. NINTINC_USD(Net interest income): 순 이자수입 (USD) - FSIBSIS 데이터셋
2-3. NINTINC_EUR(Net interest income): 순 이자수입 (EUR) - FSIBSIS 데이터셋
================================================================================
"""

import os
import sys
import logging
import traceback
import requests
import time
import json
import msvcrt
import psycopg2
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
    logger = logging.getLogger('market_imf')
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

BASE_URL = "https://api.imf.org/external/sdmx/3.0"

INDICATORS = {
    'capital_stock': {
        'code': 'CAPSTCK_PS_V_XDC',
        'name': '자본스톡 (민간, 현재가격)',
        'unit': 'Domestic currency',
        'dataflow': 'IMF.FAD/ICSD',
        'frequency': 'A',
        'key_format': 'country.indicator.frequency',  # COUNTRY.INDICATOR.FREQUENCY
        'default_period': 'last_year',
        'table': 'market_capital_stock',
        'test_table': 'test_market_capital_stock'
    },
    'net_interest_xdc': {
        'code': 'NINTINC_XDC',
        'name': '순 이자수입 (Domestic Currency)',
        'unit': 'Domestic currency',
        'dataflow': 'IMF.STA/FSIBSIS',
        'frequency': 'Q',
        'sector': 'S12CFSI',
        'key_format': 'country.sector.indicator.frequency',  # COUNTRY.SECTOR.INDICATOR.FREQUENCY
        'default_period': 'prev_two_quarters',  # 전전분기 ~ 전분기
        'table': 'market_net_interest',
        'test_table': 'test_market_net_interest'
    },
    'net_interest_usd': {
        'code': 'NINTINC_USD',
        'name': '순 이자수입 (USD)',
        'unit': 'USD',
        'dataflow': 'IMF.STA/FSIBSIS',
        'frequency': 'Q',
        'sector': 'S12CFSI',
        'key_format': 'country.sector.indicator.frequency',  # COUNTRY.SECTOR.INDICATOR.FREQUENCY
        'default_period': 'prev_two_quarters',  # 전전분기 ~ 전분기
        'table': 'market_net_interest',
        'test_table': 'test_market_net_interest'
    },
    'net_interest_eur': {
        'code': 'NINTINC_EUR',
        'name': '순 이자수입 (EUR)',
        'unit': 'EUR',
        'dataflow': 'IMF.STA/FSIBSIS',
        'frequency': 'Q',
        'sector': 'S12CFSI',
        'key_format': 'country.sector.indicator.frequency',  # COUNTRY.SECTOR.INDICATOR.FREQUENCY
        'default_period': 'prev_two_quarters',  # 전전분기 ~ 전분기
        'table': 'market_net_interest',
        'test_table': 'test_market_net_interest'
    }
}

MAX_RETRIES = 3
RETRY_DELAY = 1
REQUEST_TIMEOUT = 120


# ============================================================================
# HTTP 요청
# ============================================================================

def make_request(url, timeout=REQUEST_TIMEOUT, headers=None):
    """HTTP 요청 (재시도 포함)"""
    default_headers = {
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    if headers:
        default_headers.update(headers)

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=timeout, headers=default_headers)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print_log("ERROR", f"요청 실패: {e}")
                return None
            print_log("WARNING", f"재시도 {attempt + 1}/{MAX_RETRIES}...")
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
            json.dumps(response_json) if isinstance(response_json, dict) else response_json,
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


def save_to_db(results, batch_id, table_name='market_imf'):
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
            # 중복 체크 (period + country_code + indicator)
            cursor.execute(f"""
                SELECT 1 FROM {table_name}
                WHERE period = %s AND country_code = %s AND indicator = %s
            """, (row['period'], row['country_code'], row.get('indicator_key', '')))

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
# SDMX 3.0 JSON 파싱
# ============================================================================

def parse_sdmx_json(json_data, key_format='country.indicator.frequency'):
    """SDMX 3.0 JSON 응답 파싱

    Args:
        json_data: API JSON 응답
        key_format: 키 구조 ('country.indicator.frequency' 또는 'country.sector.indicator.frequency')

    Returns:
        list: [{'country': 'USA', 'year': '2023', 'value': 123.45, 'indicator': 'CODE'}, ...]
    """
    results = []
    try:
        datasets = json_data.get('data', {}).get('dataSets', [])
        structures = json_data.get('data', {}).get('structures', [])

        if not datasets or not structures:
            print_log("WARNING", "데이터셋 또는 구조 정보 없음")
            return []

        # 구조 정보에서 차원 값들 추출
        country_codes = []
        sector_codes = []
        indicator_codes = []
        frequency_codes = []
        time_periods = []

        for struct in structures:
            # series dimensions에서 추출
            series_dims = struct.get('dimensions', {}).get('series', [])
            for dim in series_dims:
                dim_id = dim.get('id')
                values = dim.get('values', [])
                if dim_id in ('COUNTRY', 'REF_AREA'):
                    country_codes = [v.get('id') for v in values]
                elif dim_id == 'SECTOR':
                    sector_codes = [v.get('id') for v in values]
                elif dim_id == 'INDICATOR':
                    indicator_codes = [v.get('id') for v in values]
                elif dim_id in ('FREQ', 'FREQUENCY'):
                    frequency_codes = [v.get('id') for v in values]

            # observation dimensions에서 시간 기간 추출
            obs_dims = struct.get('dimensions', {}).get('observation', [])
            for dim in obs_dims:
                if dim.get('id') == 'TIME_PERIOD':
                    time_periods = [v.get('value') for v in dim.get('values', [])]
                    break

        print_log("DEBUG", f"국가 수: {len(country_codes)}, 지표 수: {len(indicator_codes)}, 기간 수: {len(time_periods)}")

        # 관측값 추출
        for dataset in datasets:
            series = dataset.get('series', {})
            for series_key, series_data in series.items():
                # series_key에서 인덱스 추출
                key_parts = series_key.split(':')

                if key_format == 'country.sector.indicator.frequency':
                    # FSIBSIS: COUNTRY.SECTOR.INDICATOR.FREQUENCY
                    country_idx = int(key_parts[0]) if key_parts else 0
                    sector_idx = int(key_parts[1]) if len(key_parts) > 1 else 0
                    indicator_idx = int(key_parts[2]) if len(key_parts) > 2 else 0
                    freq_idx = int(key_parts[3]) if len(key_parts) > 3 else 0
                else:
                    # ICSD 등: COUNTRY.INDICATOR.FREQUENCY
                    country_idx = int(key_parts[0]) if key_parts else 0
                    indicator_idx = int(key_parts[1]) if len(key_parts) > 1 else 0
                    freq_idx = int(key_parts[2]) if len(key_parts) > 2 else 0

                country = country_codes[country_idx] if country_idx < len(country_codes) else 'UNKNOWN'
                indicator = indicator_codes[indicator_idx] if indicator_idx < len(indicator_codes) else ''

                # 관측값 처리
                observations = series_data.get('observations', {})
                for idx_str, value_list in observations.items():
                    idx = int(idx_str)
                    if idx < len(time_periods) and value_list:
                        results.append({
                            'country': country,
                            'period': time_periods[idx],
                            'value': float(value_list[0]),
                            'indicator': indicator
                        })

        print_log("DEBUG", f"파싱 결과: {len(results)}건")

    except Exception as e:
        print_log("ERROR", f"JSON 파싱 오류: {e}")
        traceback.print_exc()

    return results


# ============================================================================
# 데이터 수집
# ============================================================================

def collect_data_with_period(indicator_code, unit, dataflow, frequency='A', source_name="IMF", start_period=None, end_period=None, countries=None, key_format='country.indicator.frequency', sector=None):
    """기간 지정 데이터 수집 (SDMX 3.0 API)

    Args:
        countries: 국가 코드 리스트 (None이면 전체 국가 = 와일드카드 *)
        key_format: 키 구조 ('country.indicator.frequency' 또는 'country.sector.indicator.frequency')
        sector: 섹터 코드 (key_format이 sector 포함 시 필요)
        start_period: 시작 기간 (연도: 2023, 분기: 2023-Q1)
        end_period: 종료 기간 (연도: 2023, 분기: 2023-Q4)

    Returns:
        tuple: (data_rows, request_url, response_json)
    """
    data_rows = []

    period_desc = "전체" if start_period is None else f"{start_period}~{end_period}"
    country_desc = "전체" if countries is None else "+".join(countries)
    print_log("INFO", f"[IMF SDMX] {indicator_code} 수집 ({period_desc}, 국가: {country_desc})")

    # SDMX 3.0 URL 구성
    # /data/dataflow/{agencyID}/{resourceID}/+/{key}
    # 국가 키: None이면 * (와일드카드), 리스트면 + 로 연결
    country_key = "*" if countries is None else "+".join(countries)

    # key_format에 따라 키 구성
    if key_format == 'country.sector.indicator.frequency' and sector:
        # FSIBSIS: COUNTRY.SECTOR.INDICATOR.FREQUENCY
        key = f"{country_key}.{sector}.{indicator_code}.{frequency}"
    else:
        # ICSD 등: COUNTRY.INDICATOR.FREQUENCY
        key = f"{country_key}.{indicator_code}.{frequency}"

    base_url = f"{BASE_URL}/data/dataflow/{dataflow}/+/{key}"

    # 쿼리 파라미터
    query_parts = [
        'dimensionAtObservation=TIME_PERIOD',
        'attributes=dsd',
        'measures=all',
        'includeHistory=false'
    ]

    # 기간 필터링 - frequency 기준으로 분기
    if start_period and end_period:
        # URL 인코딩: [ → %5B, ] → %5D, + → %2B
        if frequency == 'Q':
            # 분기 데이터 (net_interest)
            # 연도만 입력된 경우 (2015) → 2015-Q1 ~ 2015-Q4로 변환
            if '-Q' not in str(start_period).upper():
                start_period = f"{start_period}-Q1"
            if '-Q' not in str(end_period).upper():
                end_period = f"{end_period}-Q4"
            query_parts.append(f'c%5BTIME_PERIOD%5D=ge:{start_period}%2Ble:{end_period}')
        else:
            # 연간 데이터 (capital_stock): 월 접미사 추가 (2015 → 2015-01, 2015-12)
            query_parts.append(f'c%5BTIME_PERIOD%5D=ge:{start_period}-01%2Ble:{end_period}-12')

    request_url = f"{base_url}?{'&'.join(query_parts)}"
    print_log("INFO", f"  요청 URL: {request_url}")

    response = make_request(request_url, timeout=120)
    if not response:
        print_log("ERROR", "API 요청 실패")
        return data_rows, request_url, None

    try:
        json_data = response.json()
        response_json = json_data

        # SDMX JSON 파싱 (key_format 전달)
        parsed_data = parse_sdmx_json(json_data, key_format=key_format)

        if not parsed_data:
            print_log("WARNING", "파싱된 데이터 없음")
            return data_rows, request_url, response_json

        # 국가별 통계
        country_stats = {}

        for row in parsed_data:
            data_rows.append({
                'period': row['period'],  # 문자열 (2023 또는 2023-Q1)
                'country_code': row['country'],
                'country_name': row['country'],
                'value': row['value'],
                'unit': unit,
                'source': source_name,
                'indicator': row.get('indicator', '')
            })
            country_stats[row['country']] = country_stats.get(row['country'], 0) + 1

        # 국가별 통계 출력
        for code, count in sorted(country_stats.items()):
            print_log("INFO", f"    {code}: {count}건")

    except (KeyError, ValueError, TypeError) as e:
        print_log("ERROR", f"파싱 오류: {e}")
        traceback.print_exc()
        return data_rows, request_url, None

    print_log("INFO", f"[IMF SDMX] 수집 완료: 총 {len(data_rows)}건")
    return data_rows, request_url, response_json


# ============================================================================
# 메인 실행
# ============================================================================

def run(mode='prod', indicators=None):
    """
    통합 실행 함수

    Args:
        mode: 실행 모드 ('dry', 'test', 'prod')
        indicators: 수집할 지표 키 (None이면 직접 실행 - 프롬프트 표시)
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
        default_period = indicator.get('default_period', 'last_year')
        selected_countries = None  # 외부 호출 시 전체 국가

        if default_period == 'last_year':
            start_period = str(current_year - 1)
            end_period = str(current_year - 1)
        elif default_period == 'prev_two_quarters':
            # 분기 데이터: 전전분기 ~ 전분기 (2개 분기)
            now = datetime.now()
            current_quarter = (now.month - 1) // 3 + 1

            # 전분기 계산
            prev_q = current_quarter - 1
            prev_year = current_year
            if prev_q <= 0:
                prev_q = 4
                prev_year = current_year - 1

            # 전전분기 계산
            prev_prev_q = prev_q - 1
            prev_prev_year = prev_year
            if prev_prev_q <= 0:
                prev_prev_q = 4
                prev_prev_year = prev_year - 1

            start_period = f"{prev_prev_year}-Q{prev_prev_q}"
            end_period = f"{prev_year}-Q{prev_q}"
        else:
            start_period = None
            end_period = None

        print(f"[지표] {indicator_key}")
        print("[국가] 전체")
        print(f"[기간] {'전체' if start_period is None else f'{start_period}~{end_period}'}")

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
        print(f"\n[국가 선택] (10초 후 전체 국가 수집)")
        print("  all 또는 엔터: 전체 국가")
        print("  USA,KOR,JPN: 특정 국가 (ISO 3자리 코드)")

        country_input = input_with_timeout("국가 입력", timeout=10)

        if not country_input or country_input.lower() == 'all':
            selected_countries = None
            print("선택된 국가: 전체")
        else:
            selected_countries = [code.strip() for code in country_input.upper().split(',') if code.strip()]
            print(f"선택된 국가: {', '.join(selected_countries)}")

        # 기간 선택 - 지표별 기본값 계산
        default_period = indicator.get('default_period', 'last_year')
        if default_period == 'prev_two_quarters':
            # 분기 데이터: 전전분기 ~ 전분기
            now = datetime.now()
            current_quarter = (now.month - 1) // 3 + 1
            prev_q = current_quarter - 1
            prev_year = current_year
            if prev_q <= 0:
                prev_q = 4
                prev_year = current_year - 1
            prev_prev_q = prev_q - 1
            prev_prev_year = prev_year
            if prev_prev_q <= 0:
                prev_prev_q = 4
                prev_prev_year = prev_year - 1
            default_start = f"{prev_prev_year}-Q{prev_prev_q}"
            default_end = f"{prev_year}-Q{prev_q}"
            default_desc = f"{default_start}~{default_end}"
        else:
            # 연간 데이터: 작년
            default_start = str(current_year - 1)
            default_end = str(current_year - 1)
            default_desc = str(current_year - 1)

        print(f"\n[기간 선택] (10초 후 기본값 적용)")
        print(f"  2015: 특정 연도")
        print("  2024-Q1: 특정 분기")
        print("  2010~2015: 범위 지정 (연도)")
        print("  2024-Q1~2024-Q4: 범위 지정 (분기)")
        print(f"  all: 전체 기간")
        print(f"  엔터: 기본값 ({default_desc})")

        period_input = input_with_timeout("기간 입력", timeout=10)

        if period_input and period_input.lower() == 'all':
            start_period = None
            end_period = None
        elif period_input and '~' in period_input:
            # 범위 지정: 2010~2015 또는 2024-Q1~2024-Q4
            parts = period_input.split('~')
            start_period = parts[0].strip()
            end_period = parts[1].strip()
        elif period_input and '-Q' in period_input.upper():
            # 단일 분기: 2024-Q1
            start_period = period_input.upper()
            end_period = period_input.upper()
        elif period_input and period_input.isdigit():
            start_period = period_input
            end_period = period_input
        else:
            # 기본값 사용 (지표별)
            start_period = default_start
            end_period = default_end

        if start_period is None:
            print("선택된 기간: 전체")
        else:
            print(f"선택된 기간: {start_period} ~ {end_period}")

    # ========================================
    # 로거 설정 (지표 선택 후)
    # ========================================
    batch_id = config['batch_prefix'] + datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = None
    if config['save_log']:
        log_file = f"imf_{indicator_key}_{batch_id}.log"
        log_path = setup_logger(log_file)
    else:
        setup_logger()

    # 헤더 출력
    print_log("INFO", "=" * 60)
    print_log("INFO", f"Market 10대 인자 - IMF SDMX [{config['name']}]")
    print_log("INFO", "=" * 60)
    print_log("INFO", f"배치 ID: {batch_id}")
    print_log("INFO", f"지표: {indicator_key} ({indicator['code']})")
    print_log("INFO", f"국가: {'전체' if selected_countries is None else ', '.join(selected_countries)}")
    if log_path:
        print_log("INFO", f"로그 파일: {log_path}")

    # ========================================
    # 데이터 수집 (공통)
    # ========================================
    print_log("INFO", "")
    print_log("INFO", "데이터 수집 시작")
    print_log("INFO", "-" * 50)

    period_desc = "전체" if start_period is None else f"{start_period}~{end_period}"
    print_log("INFO", f"{indicator_key} 수집 중... ({period_desc})")

    data, request_url, response_json = collect_data_with_period(
        indicator_code=indicator['code'],
        unit=indicator['unit'],
        dataflow=indicator['dataflow'],
        frequency=indicator.get('frequency', 'A'),
        source_name="IMF",
        start_period=start_period,
        end_period=end_period,
        countries=selected_countries,
        key_format=indicator.get('key_format', 'country.indicator.frequency'),
        sector=indicator.get('sector')
    )

    for row in data:
        row['indicator_key'] = indicator['code']

    print_log("INFO", f"{indicator_key}: {len(data)}건 수집 완료")

    # 수집 데이터 출력
    if data:
        print_log("INFO", f"조회 결과: {len(data)}건")
        print_log("INFO", "-" * 85)
        print_log("INFO", f"  {'No':<8} {'Country':<10} {'Indicator':<20} {'Period':<10} {'Value':<15} {'Unit'}")
        print_log("INFO", "-" * 85)
        sorted_data = sorted(data, key=lambda x: (x['period'], x['country_code']))
        for i, row in enumerate(sorted_data, 1):
            print_log("INFO", f"  {i:<8} {row['country_code']:<10} {indicator['code']:<20} {row['period']:<10} {row['value']:>25,} {row['unit']}")
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
        save_api_request(f'imf_{indicator_key}', batch_id, request_url, response_json)
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
    print("  t: TEST MODE (test_market_imf)")
    print("  엔터: 운영 모드 (market_imf)")
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
