"""
Market 10대 인자 - 거시경제 지표 API 통합 수집기

================================================================================
구조
================================================================================
이 파일은 개별 API 수집기를 호출하여 데이터를 통합 수집합니다.

개별 수집기:
- market_10factor_worldbank.py: World Bank API v2
- market_10factor_imf.py: IMF DataMapper API
- market_10factor_oecd.py: OECD API
- market_10factor_fred.py: FRED API

================================================================================
지표 목록 및 수집 스케줄
================================================================================
  1. 명목 GDP (PPP 기준) 1인당 - gdp_ppp_nominal (World Bank)
        Yearly, 5/1 09:00
  2. 실질 GDP (PPP 기준) 1인당 - gdp_ppp_real (World Bank)
        Yearly, 5/1 09:00
  3. 자본 스톡 (실질, PPP 기준) - capital_stock (IMF)
        Yearly, 5/15 09:00
  4. 잠재적 산출량 (PPP 기준) - potential_gdp (OECD)
        Yearly, 5/15 09:00
  5. Earnings, 개인 가처분 (실질, PPP 환산) - disposable_income_real (World Bank)
        Yearly, 5/15 09:00
  6. 소비자 물가 지수 (CPI) - cpi (World Bank)
        Yearly, 5/15 09:00
  7. 소매 가격 지수 (RPI) - rpi_usa (FRED), rpi_gbr (FRED)
        Monthly, 매월 1일 09:00
  8. Earnings, 개인 처분 가능 (명목, 국내통화단위) - disposable_income_nominal (World Bank)
        Yearly, 5/15 09:00
  9. 가계부문 기타 금융부채 (LCU) - household_debt (World Bank)
        Yearly, 5/15 09:00
  10. 민간부문 순 이자수입 (LCU) - net_interest_xdc (IMF), net_interest_usd (IMF), net_interest_eur (IMF)
        Quarterly, 1/1, 4/1, 7/1, 10/1 09:00

================================================================================
"""

import os
import sys
import logging
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 개별 수집기 임포트
from market_10factor_worldbank import run as run_worldbank
from market_10factor_worldbank import INDICATORS as WORLDBANK_INDICATORS

from market_10factor_imf import run as run_imf
from market_10factor_imf import INDICATORS as IMF_INDICATORS

from market_10factor_oecd import run as run_oecd
from market_10factor_oecd import INDICATORS as OECD_INDICATORS

from market_10factor_fred import run as run_fred
from market_10factor_fred import SERIES as FRED_SERIES

# ============================================================================
# 통합 지표 설정
# ============================================================================

INDICATORS = {
    # 1. 명목 GDP (PPP 기준) 1인당
    'gdp_ppp_nominal': {'api': 'worldbank', 'key': 'gdp_ppp_nominal'},
    # 2. 실질 GDP (PPP 기준) 1인당
    'gdp_ppp_real': {'api': 'worldbank', 'key': 'gdp_ppp_real'},
    # 3. 자본 스톡 (실질, PPP 기준)
    'capital_stock': {'api': 'imf', 'key': 'capital_stock'},
    # 4. 잠재적 산출량 (PPP 기준)
    'potential_gdp': {'api': 'oecd', 'key': 'potential_gdp'},
    # 5. Earnings, 개인 가처분 (실질, PPP 환산)
    'disposable_income_real': {'api': 'worldbank', 'key': 'disposable_income_real'},
    # 6. 소비자 물가 지수 (CPI)
    'cpi': {'api': 'worldbank', 'key': 'cpi'},
    # 7. 소매 가격 지수 (RPI)
    'rpi_usa': {'api': 'fred', 'key': 'CPIAUCSL'},
    'rpi_gbr': {'api': 'fred', 'key': 'CPRPTT01GBM661N'},
    # 8. Earnings, 개인 처분 가능 (명목, 국내통화단위)
    'disposable_income_nominal': {'api': 'worldbank', 'key': 'disposable_income_nominal'},
    # 9. 가계부문 기타 금융부채 (LCU)
    'household_debt': {'api': 'worldbank', 'key': 'household_debt'},
    # 10. 민간부문 순 이자수입 (LCU)
    'net_interest_xdc': {'api': 'imf', 'key': 'net_interest_xdc'},
    'net_interest_usd': {'api': 'imf', 'key': 'net_interest_usd'},
    'net_interest_eur': {'api': 'imf', 'key': 'net_interest_eur'},
}

# ============================================================================
# 날짜 기반 스케줄 설정
# ============================================================================
# 수집 스케줄: (월, 일) 튜플 리스트 → 해당 날짜에 수집할 지표 목록
#
# 5/1 (연간 Yearly):
#   - 명목 GDP, 실질 GDP
# 5/15 (연간 Yearly):
#   - 자본 스톡, 잠재적 산출량, 가처분소득(실질/명목), CPI, 가계부채
# 매월 1일 (월간 Monthly):
#   - RPI (미국, 영국)
# 1/1, 4/1, 7/1, 10/1 (분기 Quarterly):
#   - 순이자수입

SCHEDULE = {
    # 5/1 (연간 지표 - Yearly)
    'yearly_may_1': {
        'dates': [(5, 1)],
        'indicators': [
            'gdp_ppp_nominal',           # 명목 GDP (World Bank)
            'gdp_ppp_real',              # 실질 GDP (World Bank)
        ]
    },
    # 5/15 (연간 지표 - Yearly)
    'yearly_may_15': {
        'dates': [(5, 15)],
        'indicators': [
            'capital_stock',             # 자본 스톡 (IMF)
            'potential_gdp',             # 잠재적 산출량 (OECD)
            'disposable_income_real',    # 가처분소득 실질 (World Bank)
            'cpi',                       # 소비자물가지수 (World Bank)
            'disposable_income_nominal', # 가처분소득 명목 (World Bank)
            'household_debt',            # 가계부채 (World Bank)
        ]
    },
    # 매월 1일 (월간 지표 - Monthly)
    'monthly_1': {
        'dates': [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1),
                  (7, 1), (8, 1), (9, 1), (10, 1), (11, 1), (12, 1)],
        'indicators': ['rpi_usa', 'rpi_gbr']
    },
    # 분기 (1/1, 4/1, 7/1, 10/1) - Quarterly
    'quarterly': {
        'dates': [(1, 1), (4, 1), (7, 1), (10, 1)],
        'indicators': ['net_interest_xdc', 'net_interest_usd', 'net_interest_eur']
    },
}


def get_scheduled_indicators(target_date=None):
    """
    특정 날짜에 수집해야 할 지표 목록 반환

    Args:
        target_date: 확인할 날짜 (None이면 오늘)

    Returns:
        list: 수집해야 할 지표 키 목록
    """
    if target_date is None:
        target_date = datetime.now()

    month = target_date.month
    day = target_date.day

    scheduled = []

    for _, schedule_config in SCHEDULE.items():
        if (month, day) in schedule_config['dates']:
            for indicator in schedule_config['indicators']:
                if indicator not in scheduled:
                    scheduled.append(indicator)

    return scheduled


def get_schedule_info():
    """스케줄 정보 반환 (표시용)"""
    info = []
    for schedule_name, schedule_config in SCHEDULE.items():
        dates_str = ', '.join([f"{m}/{d}" for m, d in schedule_config['dates']])
        indicators_str = ', '.join(schedule_config['indicators'])
        info.append({
            'name': schedule_name,
            'dates': dates_str,
            'indicators': indicators_str
        })
    return info


# ============================================================================
# 로깅 설정
# ============================================================================

logger = None


def setup_logger(log_file=None):
    """로거 설정"""
    global logger
    logger = logging.getLogger('market_api')
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
# 통합 수집 함수
# ============================================================================

def collect_indicator(indicator_key):
    """
    지표 키로 데이터 수집 (전체 국가, 운영 모드)

    Args:
        indicator_key: INDICATORS 딕셔너리의 키

    Returns:
        list: 수집된 데이터 리스트
    """
    if indicator_key not in INDICATORS:
        print_log("ERROR", f"알 수 없는 지표: {indicator_key}")
        return []

    config = INDICATORS[indicator_key]
    api = config['api']
    key = config['key']

    if api == 'worldbank':
        return run_worldbank(mode='prod', indicators=[key])
    elif api == 'imf':
        return run_imf(mode='prod', indicators=[key])
    elif api == 'oecd':
        return run_oecd(mode='prod', indicators=[key])
    elif api == 'fred':
        return run_fred(mode='prod', series_id=key)
    else:
        print_log("ERROR", f"지원하지 않는 API: {api}")
        return []


def get_all_indicators():
    """전체 지표 정보 반환"""
    all_indicators = {}

    # World Bank
    for key, info in WORLDBANK_INDICATORS.items():
        all_indicators[key] = {**info, 'api': 'World Bank'}

    # IMF
    for key, info in IMF_INDICATORS.items():
        all_indicators[key] = {**info, 'api': 'IMF'}

    # OECD
    for key, info in OECD_INDICATORS.items():
        all_indicators[f'oecd_{key}'] = {**info, 'api': 'OECD'}

    # FRED
    for key, info in FRED_SERIES.items():
        all_indicators[key] = {**info, 'api': 'FRED'}

    return all_indicators


# ============================================================================
# 메인 실행
# ============================================================================

def run(target_date=None):
    """
    스케줄 기반 자동 실행 (운영 모드)

    Args:
        target_date: 기준 날짜 (None이면 오늘)

    Returns:
        dict: 수집 결과 {'indicators': [...], 'total_count': int}
    """
    if target_date is None:
        target_date = datetime.now()

    # 스케줄된 지표 확인
    scheduled_indicators = get_scheduled_indicators(target_date)

    if not scheduled_indicators:
        print_log("INFO", f"[{target_date.strftime('%Y-%m-%d')}] 오늘 수집할 지표 없음")
        return {'indicators': [], 'total_count': 0}

    batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = f"market_api_{batch_id}.log"
    log_path = setup_logger(log_file)

    # 헤더 출력
    print("\n" + "=" * 60)
    print("Market 10대 인자 - 스케줄 수집 [운영 모드]")
    print("=" * 60)
    print(f"기준 날짜: {target_date.strftime('%Y-%m-%d')}")
    print(f"배치 ID: {batch_id}")
    print(f"로그 파일: {log_path}")
    print()

    # 스케줄된 지표 목록 출력
    print("[오늘 수집 지표]")
    for i, key in enumerate(scheduled_indicators, 1):
        api = INDICATORS[key]['api'].upper()
        print(f"  {i}. {key} ({api})")
    print()

    # 데이터 수집
    print("=" * 60)
    print("데이터 수집 시작")
    print("=" * 60 + "\n")

    all_results = []

    failed_indicators = []

    for i, indicator_key in enumerate(scheduled_indicators, 1):
        print_log("INFO", f"\n[{i}/{len(scheduled_indicators)}] {indicator_key} 수집 중...")

        try:
            data = collect_indicator(indicator_key)

            for row in data:
                row['indicator_key'] = indicator_key

            all_results.extend(data)
            print_log("INFO", f"{indicator_key}: {len(data)}건 수집 완료")
        except Exception as e:
            print_log("ERROR", f"{indicator_key} 수집 실패: {e}")
            traceback.print_exc()
            failed_indicators.append(indicator_key)

    # 결과 요약
    print("\n" + "=" * 60)
    print("수집 결과 요약")
    print("=" * 60)
    print(f"총 데이터: {len(all_results)}건")

    indicator_stats = {}
    for row in all_results:
        key = row.get('indicator_key', 'unknown')
        indicator_stats[key] = indicator_stats.get(key, 0) + 1

    for key, count in indicator_stats.items():
        print(f"  {key}: {count}건")

    if failed_indicators:
        print(f"\n[실패] {', '.join(failed_indicators)}")

    # 완료 메시지
    print("\n" + "=" * 60)
    print_log("INFO", f"[운영 모드] 완료 - {len(all_results)}건 수집, {len(failed_indicators)}건 실패")
    print("=" * 60)

    return {'indicators': scheduled_indicators, 'total_count': len(all_results)}


def input_with_timeout(prompt, timeout=10):
    """타임아웃 지원 입력 (Windows)"""
    import time
    import msvcrt

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


if __name__ == "__main__":
    try:
        print("\n" + "=" * 60)
        print("Market 10대 인자 - 통합 API")
        print("=" * 60)
        print("\n[모드 선택] (10초 후 운영 모드 자동 실행)")
        print("  l: 목록 확인 (날짜별 수집 API 조회)")
        print("  엔터: 운영 모드 (오늘 스케줄 실행)")
        print()

        # 모드 입력 (10초 타임아웃)
        mode = input_with_timeout("모드 입력", timeout=10)
        today = datetime.now()

        if mode == 'l':
            # 목록 확인 모드
            print("\n" + "=" * 60)
            print("목록 확인 모드")
            print("=" * 60)
            print("날짜를 입력하세요 (예: 1/1, 5/15, 12/15)")
            print("(10초 후 자동 종료)")

            date_input = input_with_timeout("날짜 (월/일)", timeout=10)

            if date_input and '/' in date_input:
                parts = date_input.split('/')
                if len(parts) == 2:
                    try:
                        month = int(parts[0])
                        day = int(parts[1])
                        target_date = datetime(today.year, month, day)

                        indicators = get_scheduled_indicators(target_date)

                        print("\n" + "-" * 60)
                        print(f"[{month}/{day}] 수집 예정 API 목록")
                        print("-" * 60)

                        if indicators:
                            for i, ind in enumerate(indicators, 1):
                                api = INDICATORS[ind]['api'].upper()
                                print(f"  {i}. {ind} ({api})")
                        else:
                            print("  수집 예정 지표 없음")
                        print("-" * 60)
                    except ValueError:
                        print("잘못된 날짜 형식입니다.")
            elif date_input:
                print("잘못된 날짜 형식입니다. (예: 1/1, 5/15)")
            # 날짜 미입력 시 그냥 종료
        else:
            # 운영 모드 (None 또는 다른 입력)
            # 오늘 수집 예정 지표 미리보기
            scheduled = get_scheduled_indicators(today)
            print(f"\n[오늘({today.strftime('%m/%d')}) 수집 예정]")
            if scheduled:
                for ind in scheduled:
                    api = INDICATORS[ind]['api'].upper()
                    print(f"  - {ind} ({api})")
            else:
                print("  수집 예정 지표 없음")
            print()
            run()

        input("\n엔터키를 누르면 종료합니다...")
    except Exception as e:
        print(f"\n[ERROR] 예외 발생: {e}")
        traceback.print_exc()
        input("\n엔터키를 누르면 종료합니다...")
