"""
FRED API 데이터 수집기

================================================================================
API 정보
================================================================================
Base URL: https://api.stlouisfed.org/fred
문서: https://fred.stlouisfed.org/docs/api/fred/

API 키 필요: https://fred.stlouisfed.org/docs/api/api_key.html

================================================================================
시리즈 목록 (소매 가격 지수 RPI)
================================================================================
1. CPIAUCSL: 미국 CPI (Consumer Price Index for All Urban Consumers)
2. CPRPTT01GBM661N: 영국 RPI (Consumer Price Index: Retail price Index: All Items for the United Kingdom)

================================================================================
참고: 월별 데이터 제공
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

# 상위 디렉토리의 config.py 참조
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_CONFIG, FRED_API_KEY

# ============================================================================
# 로깅 설정
# ============================================================================

logger = None


def setup_logger(log_file=None):
    """로거 설정"""
    global logger
    logger = logging.getLogger('market_fred')
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

BASE_URL = "https://api.stlouisfed.org/fred"

# 시리즈 목록 (소매 가격 지수 RPI)
SERIES = {
    'CPIAUCSL': {
        'name': '미국 CPI (Consumer Price Index)',
        'country_code': 'USA',
        'unit': 'Index (1982-1984=100)',
        'frequency': 'Monthly',
        'table': 'market_rpi',
        'test_table': 'test_market_rpi'
    },
    'CPRPTT01GBM661N': {
        'name': '영국 RPI (Retail Price Index)',
        'country_code': 'GBR',
        'unit': 'Index (2015=100)',
        'frequency': 'Monthly',
        'table': 'market_rpi',
        'test_table': 'test_market_rpi'
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
    import json
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
            json.dumps(response_json) if response_json else None,
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


def save_to_db(results, batch_id, table_name='market_rpi'):
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
            """, (row['date'], row['country_code'], row['series_id']))

            if cursor.fetchone():
                skipped += 1
                continue

            # INSERT
            cursor.execute(f"""
                INSERT INTO {table_name}
                    (period, country_code, indicator, value, unit, source, batch_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                row['date'],
                row['country_code'],
                row['series_id'],
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

def collect_data_with_period(series_id, source_name="FRED", start_date=None, end_date=None):
    """기간 지정 데이터 수집

    Args:
        series_id: FRED 시리즈 ID (예: CPIAUCSL)
        source_name: 소스명
        start_date: 시작일 (YYYY-MM-DD 또는 None)
        end_date: 종료일 (YYYY-MM-DD 또는 None)

    Returns:
        tuple: (data_rows, request_url, response_json)
    """
    data_rows = []
    request_url = None
    response_json = None

    series_info = SERIES.get(series_id)
    if not series_info:
        print_log("ERROR", f"알 수 없는 시리즈: {series_id}")
        return data_rows, request_url, response_json

    # 기간 설정 (start_date/end_date가 None이면 전체 기간 조회)
    # FRED API는 기간 미지정시 전체 데이터 반환

    print_log("INFO", f"[FRED] {series_id} 수집")
    print_log("INFO", f"  시리즈: {series_info['name']}")
    print_log("INFO", f"  국가: {series_info['country_code']}")
    if start_date and end_date:
        print_log("INFO", f"  기간: {start_date} ~ {end_date}")
    else:
        print_log("INFO", f"  기간: 전체")

    # URL 생성
    request_url = f"{BASE_URL}/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
    if start_date:
        request_url += f"&observation_start={start_date}"
    if end_date:
        request_url += f"&observation_end={end_date}"
    print_log("INFO", f"  요청 URL: {request_url[:80]}...")

    response = make_request(request_url)
    if not response:
        print_log("ERROR", "API 요청 실패")
        return data_rows, request_url, response_json

    try:
        json_data = response.json()
        response_json = json_data

        if 'observations' not in json_data:
            print_log("WARNING", "관측값 없음")
            return data_rows, request_url, response_json

        observations = json_data['observations']

        for obs in observations:
            if obs['value'] != '.' and obs['value'] is not None:
                try:
                    value = float(obs['value'])
                    date_str = obs['date']  # YYYY-MM-DD 형식

                    data_rows.append({
                        'date': date_str,
                        'country_code': series_info['country_code'],
                        'series_id': series_id,
                        'value': value,
                        'unit': series_info['unit'],
                        'source': source_name
                    })
                except (ValueError, TypeError):
                    continue

        print_log("INFO", f"  수집 완료: {len(data_rows)}건")

    except (KeyError, ValueError, TypeError) as e:
        print_log("ERROR", f"파싱 오류: {e}")
        traceback.print_exc()

    return data_rows, request_url, response_json


# ============================================================================
# 메인 실행
# ============================================================================

def run(mode='prod', series_id=None):
    """
    통합 실행 함수

    Args:
        mode: 실행 모드 ('dry', 'test', 'prod')
        series_id: 수집할 시리즈 ID (None이면 직접 실행 - 프롬프트 표시)
    """
    # 외부 호출 여부 판단: series_id가 있으면 외부 호출
    is_external_call = series_id is not None

    config = MODE_CONFIG.get(mode, MODE_CONFIG['prod'])
    series_list = list(SERIES.keys())

    # ========================================
    # 파라미터 결정
    # ========================================
    if is_external_call:
        # 외부 호출: 지정된 시리즈, 전체 기간
        selected_series = series_id
        series_info = SERIES[selected_series]
        start_date = None
        end_date = None

        print(f"[시리즈] {selected_series}: {series_info['name']}")
        print(f"[기간] 전체")

    else:
        # 직접 실행: 프롬프트로 선택
        print("[수집 가능 시리즈]")
        for i, sid in enumerate(series_list, 1):
            info = SERIES[sid]
            print(f"  {i}. {sid}: {info['name']} ({info['country_code']})")
        print()

        print("[시리즈 선택] 번호 입력")
        series_input = input("시리즈 입력: ").strip()

        if not series_input.isdigit():
            print_log("ERROR", "번호를 입력해주세요")
            return []

        idx = int(series_input) - 1
        if idx < 0 or idx >= len(series_list):
            print_log("ERROR", "잘못된 번호입니다")
            return []

        selected_series = series_list[idx]
        series_info = SERIES[selected_series]
        print(f"선택된 시리즈: {selected_series} ({series_info['name']})")

        # 기간 선택 - 기본값: 전월
        now = datetime.now()
        if now.month == 1:
            prev_month = f"{now.year - 1}-12"
        else:
            prev_month = f"{now.year}-{now.month - 1:02d}"

        print(f"\n[기간 선택] (10초 후 기본값: 전월)")
        print(f"  2024: 특정 연도 (2024-01 ~ 2024-12)")
        print(f"  2024-06: 특정 월")
        print(f"  2020~2024: 연도 범위 지정")
        print(f"  2024-01~2024-06: 월 범위 지정")
        print(f"  all: 전체 기간")
        print(f"  엔터: 기본값 ({prev_month})")

        period_input = input_with_timeout("기간 입력", timeout=10)

        if period_input and period_input.lower() == 'all':
            start_date = None
            end_date = None
        elif period_input and '~' in period_input:
            parts = period_input.split('~')
            start_part = parts[0].strip()
            end_part = parts[1].strip()
            # 월 형식(YYYY-MM) 또는 연도 형식(YYYY) 처리
            if '-' in start_part:
                start_date = f"{start_part}-01"  # YYYY-MM-01
            else:
                start_date = f"{start_part}-01-01"  # YYYY-01-01
            if '-' in end_part:
                end_date = f"{end_part}-28"  # 월말
            else:
                end_date = f"{end_part}-12-31"
        elif period_input and '-' in period_input:
            # 특정 월 (YYYY-MM)
            start_date = f"{period_input}-01"
            end_date = f"{period_input}-28"
        elif period_input and period_input.isdigit():
            # 특정 연도 (YYYY)
            start_date = f"{period_input}-01-01"
            end_date = f"{period_input}-12-31"
        else:
            # 기본값: 전월
            start_date = f"{prev_month}-01"
            end_date = f"{prev_month}-28"

        if start_date is None:
            print("선택된 기간: 전체")
        else:
            print(f"선택된 기간: {start_date} ~ {end_date}")

    # ========================================
    # 로거 설정
    # ========================================
    batch_id = config['batch_prefix'] + datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = None
    if config['save_log']:
        log_file = f"fred_{selected_series}_{batch_id}.log"
        log_path = setup_logger(log_file)
    else:
        setup_logger()

    # 헤더 출력
    print_log("INFO", "=" * 60)
    print_log("INFO", f"Market 10대 인자 - FRED [{config['name']}]")
    print_log("INFO", "=" * 60)
    print_log("INFO", f"배치 ID: {batch_id}")
    print_log("INFO", f"시리즈: {selected_series} ({series_info['name']})")
    print_log("INFO", f"국가: {series_info['country_code']}")
    if log_path:
        print_log("INFO", f"로그 파일: {log_path}")

    # ========================================
    # 데이터 수집
    # ========================================
    print_log("INFO", "")
    print_log("INFO", "데이터 수집 시작")
    print_log("INFO", "-" * 50)

    data, request_url, response_json = collect_data_with_period(
        series_id=selected_series,
        source_name="FRED",
        start_date=start_date,
        end_date=end_date
    )

    print_log("INFO", f"{selected_series}: {len(data)}건 수집 완료")

    # 수집 데이터 출력
    if data:
        # 날짜 범위 계산
        dates = [row['date'] for row in data]
        print_log("INFO", f"조회 결과: {len(data)}건")
        print_log("INFO", f"기간: {min(dates)} ~ {max(dates)}")
        print_log("INFO", "-" * 90)
        print_log("INFO", f"  {'No':<6} {'Date':<12} {'Country':<8} {'Indicator':<18} {'Value':>15} {'Unit'}")
        print_log("INFO", "-" * 90)
        sorted_data = sorted(data, key=lambda x: x['date'])
        for i, row in enumerate(sorted_data, 1):
            print_log("INFO", f"  {i:<6} {row['date']:<12} {row['country_code']:<8} {row['series_id']:<18} {row['value']:>15,.5f} {row['unit']}")
        print_log("INFO", "-" * 90)

    # ========================================
    # DB 저장
    # ========================================
    if mode == 'dry':
        table_name = None
    elif mode == 'test':
        table_name = series_info.get('test_table')
    else:
        table_name = series_info.get('table')

    if table_name and data:
        print("\n" + "-" * 60)
        save_api_request(f'fred_{selected_series}', batch_id, request_url, response_json)
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
    print("  t: TEST MODE (test_market_rpi)")
    print("  엔터: 운영 모드 (market_rpi)")
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
