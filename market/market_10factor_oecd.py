"""
OECD Economic Outlook API 데이터 수집기

================================================================================
데이터 소스
================================================================================
- OECD Economic Outlook 117 (Long-Term Baseline)
- 지표: GDPVTRD (Potential gross domestic product, USD PPP)
- Measure: Potential gross domestic product, volume, USD at 2021 Purchasing Power Parities
- Scenario: All
    - Business-as-usual, median climate damage, no carbon mitigation (BAU1)
    - Business-as-usual, high damage curve, no carbon mitigation (BAU2)
    - Accelerated transition, median damage, slow carbon reduction (ET1)
    - Accelerated transition, median damage, fast carbon reduction (ET2)
    - Accelerated transition, high damage, slow carbon reduction (ET3)
    - Accelerated transition, high damage, fast carbon reduction (ET4)

================================================================================
API 정보
================================================================================
Base URL: https://sdmx.oecd.org/public/rest
문서: 
    - https://data-explorer.oecd.org
    - https://data-explorer.oecd.org/vis?tm=Economic%20Outlook%20117&pg=0&fc=Measure&snb=1&fs[0]=Frequency%20of%20observation%2C0%7CAnnual%23A%23&fs[1]=Measure%2C1%7CSupply%20block%23SUP%23%7CPotential%20gross%20domestic%20product%252C%20volume%2C%20USD%20at%202021%20Purchasing%20Power%20Parities%23GDPVTRD%23&vw=tb&df[ds]=dsDisseminateFinalDMZ&df[id]=DSD_EO_LTB%40DF_EO_LTB&df[ag]=OECD.ECO.MAD&df[vs]=1.0&dq=.GDPVTRD..A&pd=2026%2C2030&to[TIME_PERIOD]=false

================================================================================
지표 목록
================================================================================
1. GDPVTRD: 잠재적 산출량 (Potential gross domestic product, USD PPP)

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
from datetime import datetime

from psycopg2.extras import Json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_CONFIG

# ============================================================================
# 로깅 설정
# ============================================================================

logger = None


def setup_logger(log_file=None):
    """로거 설정"""
    global logger
    logger = logging.getLogger('market_oecd')
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

BASE_URL = "https://sdmx.oecd.org/public/rest/data"
DEFAULT_DATAFLOW = "OECD.ECO.MAD,DSD_EO_LTB@DF_EO_LTB,1.0"

INDICATORS = {
    'potential_gdp': {
        'code': 'GDPVTRD',
        'name': '잠재적 산출량 (Potential GDP)',
        'unit': 'USD at 2021 PPP',
        'frequency': 'A',
        'default_period': 'future_5years',  # 내년부터 5년
        'table': 'market_potential_gdp',
        'test_table': 'test_market_potential_gdp'
    }
}

MAX_RETRIES = 3
RETRY_DELAY = 1
REQUEST_TIMEOUT = 60


# ============================================================================
# HTTP 요청
# ============================================================================

def make_request(url, timeout=REQUEST_TIMEOUT):
    """HTTP 요청 (재시도 포함)"""
    headers = {
        'Accept': 'application/vnd.sdmx.data+json; charset=utf-8; version=1.0',
        'Accept-Encoding': 'gzip, deflate',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
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
            Json(response_json) if response_json else None,
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


def save_to_db(results, batch_id, indicator, table_name='market_potential_gdp'):
    """DB 저장 (전망치는 중복 체크 없이 모두 저장)

    Args:
        results: 저장할 데이터 리스트
        batch_id: 배치 ID
        indicator: 지표 딕셔너리 (code, unit 등 포함)
        table_name: 테이블명
    """
    if not results:
        print_log("WARNING", "저장할 데이터 없음")
        return False

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        created_at = datetime.now()

        inserted = 0

        for row in results:
            cursor.execute(f"""
                INSERT INTO {table_name}
                    (period, country_code, indicator, scenario, value, unit, source, batch_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                row['period'],
                row['country_code'],
                indicator['code'],
                row.get('scenario', ''),
                row['value'],
                indicator['unit'],
                row.get('source', 'OECD'),
                batch_id,
                created_at
            ))
            inserted += 1

        conn.commit()
        cursor.close()
        conn.close()

        print_log("INFO", f"DB 저장 완료 ({table_name}): INSERT {inserted}건")
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

def collect_data_with_period(indicator_code, unit, source_name="OECD", countries=None, start_year=None, end_year=None, scenarios=None):
    """기간 지정 데이터 수집 (SDMX JSON API)

    Returns:
        tuple: (data_rows, request_url, response_json)
    """
    data_rows = []
    request_url = None
    response_json = None

    period_desc = "전체" if start_year is None else f"{start_year}~{end_year}"
    print_log("INFO", f"[OECD Potential GDP] {indicator_code} 수집 ({period_desc})")

    # 국가 코드
    if countries is None:
        country_key = ''
        print_log("INFO", "  대상 국가: 전체")
    else:
        country_key = '+'.join(countries)
        print_log("INFO", f"  대상 국가: {', '.join(countries)}")

    # 시나리오
    if scenarios and isinstance(scenarios, list):
        scenario_key = '+'.join(scenarios)
        print_log("INFO", f"  시나리오: {', '.join(scenarios)}")
    else:
        scenario_key = ''
        print_log("INFO", "  시나리오: 전체")

    # 키 구성: 국가.지표.시나리오.빈도
    key = f"{country_key}.{indicator_code}.{scenario_key}.A"

    # URL 구성
    url = f"{BASE_URL}/{DEFAULT_DATAFLOW}/{key}"

    params = ['dimensionAtObservation=AllDimensions']
    if start_year:
        params.append(f'startPeriod={start_year}')
    if end_year:
        params.append(f'endPeriod={end_year}')

    request_url = f"{url}?{'&'.join(params)}"
    print_log("INFO", f"  요청 URL: {request_url}")

    response = make_request(request_url, timeout=120)
    if not response:
        print_log("ERROR", "API 요청 실패")
        return data_rows, request_url, None

    try:
        json_data = response.json()
        response_json = json_data

        # SDMX-JSON 파싱
        data_rows = parse_sdmx_json(json_data, unit, source_name)

        if not data_rows:
            print_log("WARNING", "파싱된 데이터 없음")
            return data_rows, request_url, response_json

        # 국가별 통계
        country_stats = {}
        for row in data_rows:
            code = row['country_code']
            country_stats[code] = country_stats.get(code, 0) + 1

        for code, count in sorted(country_stats.items()):
            print_log("INFO", f"    {code}: {count}건")

    except Exception as e:
        print_log("ERROR", f"파싱 오류: {e}")
        traceback.print_exc()
        return data_rows, request_url, None

    print_log("INFO", f"[OECD Potential GDP] 수집 완료: 총 {len(data_rows)}건")
    return data_rows, request_url, response_json


def parse_sdmx_json(json_data, unit, source_name):
    """SDMX-JSON 응답 파싱"""
    results = []
    try:
        data_obj = json_data.get('data', json_data)
        datasets = data_obj.get('dataSets', [])
        structures = data_obj.get('structures', [{}])[0] if data_obj.get('structures') else data_obj.get('structure', {})

        if not datasets:
            print_log("WARNING", "dataSets not found in response")
            return []

        dimensions = structures.get('dimensions', {}).get('observation', [])

        dim_map = {}
        dim_positions = {}

        for pos, dim in enumerate(dimensions):
            dim_id = dim.get('id')
            dim_positions[dim_id] = pos
            dim_map[dim_id] = {}
            for idx, val in enumerate(dim.get('values', [])):
                dim_map[dim_id][idx] = val.get('id')

        for dataset in datasets:
            observations = dataset.get('observations', {})

            for obs_key, obs_values in observations.items():
                key_parts = [int(k) for k in obs_key.split(':')]

                ref_area = dim_map.get('REF_AREA', {}).get(key_parts[dim_positions.get('REF_AREA', 0)], '')
                scenario = dim_map.get('SCENARIO', {}).get(key_parts[dim_positions.get('SCENARIO', 2)], '')
                time_period = dim_map.get('TIME_PERIOD', {}).get(key_parts[dim_positions.get('TIME_PERIOD', 4)], '')

                value = obs_values[0] if obs_values else None

                if ref_area and time_period and value is not None:
                    results.append({
                        'period': str(time_period),
                        'country_code': ref_area,
                        'scenario': scenario,
                        'value': value,
                        'unit': unit,
                        'source': source_name
                    })

        results.sort(key=lambda x: (x['country_code'], x['scenario'], x['period']))
        print_log("DEBUG", f"파싱 결과: {len(results)}건")

    except Exception as e:
        print_log("ERROR", f"JSON 파싱 오류: {e}")
        traceback.print_exc()

    return results


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
        selected_countries = None
        selected_scenarios = None
        default_period = indicator.get('default_period', 'future_5years')

        if default_period == 'future_5years':
            start_year = current_year + 1
            end_year = current_year + 5
        else:
            start_year = None
            end_year = None

        print(f"[지표] {indicator_key}")
        print("[국가] 전체")
        print("[시나리오] 전체")
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

        # 시나리오 선택
        print("\n[시나리오 선택] (10초 후 전체 시나리오)")
        print("  BAU1,BAU2,ET1,ET2,ET3,ET4: 개별 선택")
        print("  엔터: 전체 시나리오")

        scenario_input = input_with_timeout("시나리오 입력", timeout=10)

        if not scenario_input:
            selected_scenarios = None
            print("선택된 시나리오: 전체")
        else:
            selected_scenarios = [s.strip().upper() for s in scenario_input.split(',') if s.strip()]
            print(f"선택된 시나리오: {', '.join(selected_scenarios)}")

        # 기간 선택
        next_year = current_year + 1
        default_end = current_year + 5

        print(f"\n[기간 선택] (10초 후 기본값 적용)")
        print(f"  2027: 특정 연도")
        print("  2026~2035: 범위 지정")
        print(f"  all: 전체 기간")
        print(f"  엔터: 기본값 ({next_year}~{default_end})")

        period_input = input_with_timeout("기간 입력", timeout=10)

        if period_input and period_input.lower() == 'all':
            start_year = None
            end_year = None
        elif period_input and '~' in period_input:
            parts = period_input.split('~')
            start_year = int(parts[0].strip())
            end_year = int(parts[1].strip())
        elif period_input and period_input.isdigit():
            start_year = int(period_input)
            end_year = int(period_input)
        else:
            start_year = next_year
            end_year = default_end

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
        log_file = f"oecd_{indicator_key}_{batch_id}.log"
        log_path = setup_logger(log_file)
    else:
        setup_logger()

    # 헤더 출력
    print_log("INFO", "=" * 60)
    print_log("INFO", f"Market 10대 인자 - OECD [{config['name']}]")
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

    period_desc = "전체" if start_year is None else f"{start_year}~{end_year}"
    print_log("INFO", f"{indicator_key} 수집 중... ({period_desc})")

    data, request_url, response_json = collect_data_with_period(
        indicator_code=indicator['code'],
        unit=indicator['unit'],
        source_name="OECD",
        countries=selected_countries,
        start_year=start_year,
        end_year=end_year,
        scenarios=selected_scenarios
    )

    for row in data:
        row['indicator_key'] = indicator['code']

    print_log("INFO", f"{indicator_key}: {len(data)}건 수집 완료")

    # 수집 데이터 출력
    if data:
        scenarios_set = set(row['scenario'] for row in data)
        countries_set = set(row['country_code'] for row in data)
        periods = [int(row['period']) for row in data]

        print_log("INFO", f"조회 결과: {len(data)}건")
        print_log("INFO", f"국가 수: {len(countries_set)}")
        print_log("INFO", f"시나리오 수: {len(scenarios_set)} ({', '.join(sorted(scenarios_set))})")
        print_log("INFO", f"기간 범위: {min(periods)} ~ {max(periods)}")

        print_log("INFO", "-" * 120)
        print_log("INFO", f"  {'No':<6} {'Period':<8} {'Country':<10} {'Indicator':<12} {'Scenario':<10} {'Value':>25} {'Unit'}")
        print_log("INFO", "-" * 120)
        sorted_data = sorted(data, key=lambda x: (x['period'], x['country_code'], x['scenario']))
        for i, row in enumerate(sorted_data[:50], 1):
            print_log("INFO", f"  {i:<6} {row['period']:<8} {row['country_code']:<10} {indicator['code']:<12} {row['scenario']:<10} {row['value']:>25} {row.get('unit', '')}")
        if len(sorted_data) > 50:
            print_log("INFO", f"  ... 외 {len(sorted_data) - 50}건")
        print_log("INFO", "-" * 120)

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
        save_api_request(f'oecd_{indicator_key}', batch_id, request_url, response_json)
        save_to_db(data, batch_id, indicator, table_name=table_name)

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
    print("  t: TEST MODE (test_market_oecd)")
    print("  엔터: 운영 모드 (market_oecd)")
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
