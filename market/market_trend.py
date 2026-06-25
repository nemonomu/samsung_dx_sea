"""
Market Trend Crawler - Base Module

Bing 뉴스 검색 기반 마켓 트렌드 크롤러
키워드별 기사 수 수집 (undetected-chromedriver 기반)

================================================================================
실행 모드
================================================================================
- 운영 모드: 10초 내 입력 없으면 자동 실행 (market_trend 테이블에 저장)
- 테스트 모드: 't' 입력 시 실행 (test_market_trend 테이블에 저장)

================================================================================
"""

import os
import sys
import time
import random
import re
import traceback
import logging
import glob
import subprocess
import psycopg2
import msvcrt
from datetime import datetime
from lxml import html
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait

# 상위 디렉토리의 config.py 참조
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB_CONFIG

# ============================================================================
# 로그 설정
# ============================================================================

# 로그 디렉토리 및 파일 설정 (루트/logs 폴더)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, 'logs')
LOG_FILE = None
logger = None


def setup_logger():
    """로거 설정 (파일 + 콘솔 출력)"""
    global LOG_FILE, logger

    # 로그 디렉토리 생성
    os.makedirs(LOG_DIR, exist_ok=True)

    # 로그 파일명 생성 (market_trend_YYYYMMDD_HHMMSS.log)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    LOG_FILE = os.path.join(LOG_DIR, f'market_trend_{timestamp}.log')

    # 로거 생성
    logger = logging.getLogger('market_trend')
    logger.setLevel(logging.DEBUG)

    # 기존 핸들러 제거
    logger.handlers.clear()

    # 파일 핸들러 (UTF-8 인코딩)
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(file_handler)

    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(console_handler)

    return LOG_FILE


def cleanup_old_logs(days=30):
    """오래된 로그 파일 정리 (N일 이전 로그 삭제)"""
    try:
        log_pattern = os.path.join(LOG_DIR, 'market_trend_*.log')
        log_files = glob.glob(log_pattern)
        now = datetime.now()

        for log_file in log_files:
            file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
            age_days = (now - file_mtime).days

            if age_days > days:
                os.remove(log_file)
                print_log("INFO", f"오래된 로그 삭제: {os.path.basename(log_file)} ({age_days}일 전)")
    except Exception as e:
        print_log("WARNING", f"로그 정리 실패: {e}")


# ============================================================================
# 유틸리티 함수
# ============================================================================

def get_timestamp():
    """현재 시간 반환 (YYYY-MM-DD HH:MM:SS)"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def print_log(level, message):
    """로그 출력 (파일 + 콘솔)"""
    if logger:
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(message)
    else:
        timestamp = get_timestamp()
        print(f"[{timestamp}] [{level}] {message}")


def extract_number(text):
    """
    텍스트에서 숫자 추출
    예: "약 1,234개의 결과" -> 1234
    """
    if not text:
        return None

    # 쉼표 제거하고 숫자만 추출
    numbers = re.findall(r'[\d,]+', text)
    if numbers:
        # 첫 번째 숫자 반환 (쉼표 제거)
        return int(numbers[0].replace(',', ''))
    return None


def get_input_with_timeout(prompt, timeout=10):
    """
    타임아웃이 있는 입력 받기 (Windows용)

    Args:
        prompt: 프롬프트 메시지
        timeout: 타임아웃 (초)

    Returns:
        str or None: 입력값 또는 타임아웃 시 None
    """
    print(prompt, end='', flush=True)

    start_time = time.time()
    input_chars = []

    while True:
        # 남은 시간 계산
        elapsed = time.time() - start_time
        remaining = timeout - elapsed

        if remaining <= 0:
            print()  # 줄바꿈
            return None

        # 키 입력 확인 (Windows)
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == '\r':  # Enter
                print()  # 줄바꿈
                return ''.join(input_chars)
            elif char == '\b':  # Backspace
                if input_chars:
                    input_chars.pop()
                    print('\b \b', end='', flush=True)
            else:
                input_chars.append(char)
                print(char, end='', flush=True)

        time.sleep(0.1)


# ============================================================================
# 데이터베이스 클래스
# ============================================================================

class DatabaseManager:
    """데이터베이스 연결 및 쿼리 관리"""

    def __init__(self, test_mode=False):
        self.conn = None
        self.cursor = None
        self.test_mode = test_mode
        self.table_name = 'test_market_trend' if test_mode else 'market_trend'

    def connect(self):
        """DB 연결"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG, database='postgres')
            self.cursor = self.conn.cursor()
            print_log("INFO", f"DB 연결 완료 (테이블: {self.table_name})")
            return True
        except Exception as e:
            print_log("ERROR", f"DB 연결 실패: {e}")
            return False

    def disconnect(self):
        """DB 연결 해제"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        print_log("INFO", "DB 연결 해제")

    def execute(self, query, params=None):
        """쿼리 실행"""
        try:
            self.cursor.execute(query, params)
            return True
        except Exception as e:
            print_log("ERROR", f"쿼리 실행 실패: {e}")
            return False

    def fetchall(self):
        """모든 결과 반환"""
        return self.cursor.fetchall()

    def fetchone(self):
        """단일 결과 반환"""
        return self.cursor.fetchone()

    def commit(self):
        """커밋"""
        self.conn.commit()

    def rollback(self):
        """롤백"""
        self.conn.rollback()

    def get_keywords(self, product_line=None, content_type=None, limit=None):
        """
        키워드 목록 조회 (product_line, content_type 포함)

        Args:
            product_line: 제품 라인 필터 (None이면 전체) - TV, HHP
            content_type: 콘텐츠 유형 필터 (None이면 전체) - News, Event
            limit: 조회 개수 제한 (None이면 전체)
        """
        query = """
            SELECT keyword, search_url, product_line, content_type
            FROM market_mst
            WHERE is_active = true AND analysis_type = 'trend'
        """
        params = []

        if product_line:
            query += " AND product_line = %s"
            params.append(product_line)

        if content_type:
            query += " AND content_type = %s"
            params.append(content_type)

        query += " ORDER BY id"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        self.execute(query, params if params else None)
        return self.fetchall()

    def save_article_count(self, keyword, article_count, calendar_week, crawl_datetime):
        """기사 수 저장 (테스트 모드에 따라 테이블 변경)"""
        query = f"""
            INSERT INTO {self.table_name} (keyword, total_article_number, calendar_week, crawl_at_local_time)
            VALUES (%s, %s, %s, %s)
        """
        return self.execute(query, (keyword, article_count, calendar_week, crawl_datetime))

    def save_batch_with_retry(self, results, calendar_week):
        """
        배치 단위로 저장 (20 → 5 → 1 재시도)

        Args:
            results: 크롤링 결과 리스트 [{keyword, article_count, crawl_datetime}, ...]
            calendar_week: 캘린더 주차

        Returns:
            tuple: (성공 수, 실패 수)
        """
        insert_query = f"""
            INSERT INTO {self.table_name} (keyword, total_article_number, calendar_week, crawl_at_local_time)
            VALUES (%s, %s, %s, %s)
        """

        def result_to_tuple(r):
            return (r['keyword'], r['article_count'], calendar_week, r['crawl_datetime'])

        total_success = 0
        total_fail = 0
        BATCH_SIZE = 20
        SUB_BATCH_SIZE = 5

        for batch_start in range(0, len(results), BATCH_SIZE):
            batch = results[batch_start:batch_start + BATCH_SIZE]

            # 1차: 20개 배치 저장
            try:
                values_list = [result_to_tuple(r) for r in batch]
                self.cursor.executemany(insert_query, values_list)
                self.commit()
                total_success += len(batch)
                continue
            except Exception:
                self.rollback()

            # 2차: 5개씩 분할 저장
            for sub_start in range(0, len(batch), SUB_BATCH_SIZE):
                sub_batch = batch[sub_start:sub_start + SUB_BATCH_SIZE]

                try:
                    values_list = [result_to_tuple(r) for r in sub_batch]
                    self.cursor.executemany(insert_query, values_list)
                    self.commit()
                    total_success += len(sub_batch)
                except Exception:
                    self.rollback()

                    # 3차: 1개씩 개별 저장
                    for result in sub_batch:
                        try:
                            self.cursor.execute(insert_query, result_to_tuple(result))
                            self.commit()
                            total_success += 1
                        except Exception as e:
                            print_log("ERROR", f"저장 실패: {result['keyword']}: {e}")
                            query = self.cursor.mogrify(insert_query, result_to_tuple(result))
                            print_log("DEBUG", f"Query: {query.decode('utf-8')}")
                            self.rollback()
                            total_fail += 1

        return total_success, total_fail


    def get_all_xpaths(self):
        """모든 XPath 조회 (딕셔너리로 반환)"""
        query = """
            SELECT data_field, xpath
            FROM market_xpath_selectors
            WHERE is_active = true
        """
        self.execute(query)
        results = self.fetchall()
        return {row[0]: row[1] for row in results}


# ============================================================================
# 브라우저 클래스 (undetected-chromedriver 기반)
# ============================================================================

class BrowserManager:
    """undetected-chromedriver 브라우저 관리"""

    def __init__(self, headless=False):
        self.headless = headless
        self.driver = None
        self.wait = None

    def get_chrome_version(self):
        """설치된 Chrome 브라우저의 메이저 버전 감지"""
        try:
            result = subprocess.run(
                ['reg', 'query', 'HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon', '/v', 'version'],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                match = re.search(r'(\d+)\.\d+\.\d+\.\d+', result.stdout)
                if match:
                    version = int(match.group(1))
                    print_log("INFO", f"감지된 Chrome 버전: {version}")
                    return version
        except Exception as e:
            print_log("WARNING", f"Chrome 버전 감지 실패: {e}")
        return None

    def setup(self):
        """undetected-chromedriver 설정"""
        print_log("INFO", "undetected-chromedriver 설정 중...")

        try:
            # Chrome 버전 자동 감지
            chrome_version = self.get_chrome_version()

            options = uc.ChromeOptions()

            # 페이지 로드 전략
            options.page_load_strategy = 'none'

            # 기본 옵션
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-gpu')
            options.add_argument('--window-size=1920,1080')
            options.add_argument('--lang=en-US,en;q=0.9')
            options.add_argument('--start-maximized')

            # 알림 비활성화
            prefs = {
                "profile.default_content_setting_values.notifications": 2,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            }
            options.add_experimental_option("prefs", prefs)

            # headless 모드
            if self.headless:
                options.add_argument('--headless=new')

            # undetected_chromedriver 사용 (Chrome 버전 자동 감지)
            self.driver = uc.Chrome(options=options, version_main=chrome_version)
            self.driver.set_page_load_timeout(120)
            self.wait = WebDriverWait(self.driver, 20)

            print_log("INFO", "undetected-chromedriver 설정 완료")
            return True

        except Exception as e:
            print_log("ERROR", f"드라이버 설정 실패: {e}")
            traceback.print_exc()
            return False

    def close(self):
        """브라우저 종료"""
        if self.driver:
            self.driver.quit()
        print_log("INFO", "브라우저 종료")

    def goto(self, url):
        """페이지 이동"""
        self.driver.get(url)

    def get_content(self):
        """페이지 HTML 반환"""
        return self.driver.page_source

    def wait_random(self, min_sec=2, max_sec=5):
        """랜덤 대기"""
        wait_time = random.uniform(min_sec, max_sec)
        time.sleep(wait_time)

    def scroll_to_bottom(self, scroll_step=300, max_scrolls=200):
        """
        페이지 하단까지 점진적 스크롤 (footer가 보일 때까지)

        Args:
            scroll_step: 한 번에 스크롤할 픽셀 (기본 300px)
            max_scrolls: 최대 스크롤 횟수 (무한 스크롤 방지)
        """
        try:
            current_position = 0
            scroll_count = 0

            while scroll_count < max_scrolls:
                # 점진적 스크롤 (300px씩)
                current_position += scroll_step
                self.driver.execute_script(f"window.scrollTo(0, {current_position});")

                # 대기
                time.sleep(random.uniform(0.5, 1.0))

                scroll_count += 1

                # footer 요소 확인 (Bing News 페이지의 footer)
                footer_visible = self.driver.execute_script("""
                    var footer = document.querySelector('footer#b_footer, footer.b_footer');
                    if (footer) {
                        var rect = footer.getBoundingClientRect();
                        return rect.top < window.innerHeight;
                    }
                    return false;
                """)

                if footer_visible:
                    print_log("INFO", f"  Footer 감지 - 스크롤 완료 ({scroll_count}회, {current_position}px)")
                    break

            # 스크롤 완료 후 추가 대기 (모든 컨텐츠 로딩 완료 대기)
            time.sleep(random.uniform(2, 3))

        except Exception as e:
            print_log("WARNING", f"스크롤 실패: {e}")


# ============================================================================
# Bing News 크롤러
# ============================================================================

class BingNewsCrawler:
    """Bing News 검색 기사 수 크롤러"""

    def __init__(self, headless=False, test_mode=False, test_product_line=None, test_content_type=None, test_count=None):
        self.test_mode = test_mode
        self.test_product_line = test_product_line
        self.test_content_type = test_content_type
        self.test_count = test_count
        self.db = DatabaseManager(test_mode=test_mode)
        self.browser = BrowserManager(headless=headless)
        self.xpaths = {}

    def setup(self):
        """초기화"""
        if not self.db.connect():
            return False

        # DB에서 XPath 로드
        self.xpaths = self.db.get_all_xpaths()
        if not self.xpaths:
            print_log("ERROR", "XPath 설정을 찾을 수 없습니다.")
            return False

        print_log("INFO", f"XPath {len(self.xpaths)}개 로드됨")

        if not self.browser.setup():
            return False

        return True

    def cleanup(self):
        """정리"""
        self.browser.close()
        self.db.disconnect()

    def generate_calendar_week(self):
        """
        캘린더 주차 생성 (예: W47)

        Returns:
            str: 캘린더 주차 (예: 'W47')
        """
        now = datetime.now()
        week_number = now.isocalendar()[1]
        return f"W{week_number:02d}"


    def count_articles(self, tree):
        """
        기사 컨테이너 수 카운트 (페이지의 모든 기사)

        Args:
            tree: lxml HTML tree

        Returns:
            int: 기사 수
        """
        try:
            container_xpath = self.xpaths.get('news_card_container')

            if not container_xpath:
                print_log("ERROR", "news_card_container XPath가 없습니다.")
                return None

            news_cards = tree.xpath(container_xpath)
            total_count = len(news_cards)

            print_log("INFO", f"  -> 기사 수: {total_count}개")
            return total_count

        except Exception as e:
            print_log("WARNING", f"기사 카운트 실패: {e}")
            return 0

    def crawl_keyword(self, keyword, search_url):
        """단일 키워드 크롤링"""
        try:
            print_log("INFO", f"크롤링: {keyword}")

            self.browser.goto(search_url)
            self.browser.wait_random(3, 5)
            self.browser.scroll_to_bottom()

            page_html = self.browser.get_content()
            tree = html.fromstring(page_html)
            article_count = self.count_articles(tree)

            # 크롤링 완료 시점 기록
            crawl_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            return {'keyword': keyword, 'article_count': article_count, 'crawl_datetime': crawl_datetime, 'success': True}

        except Exception as e:
            print_log("ERROR", f"크롤링 실패 ({keyword}): {e}")
            traceback.print_exc()
            return {'keyword': keyword, 'article_count': None, 'crawl_datetime': None, 'success': False}

    def crawl_all_keywords(self, keywords, calendar_week):
        """
        모든 키워드 크롤링 및 배치 저장

        Args:
            keywords: 키워드 목록 [(keyword, search_url, category1, category2), ...]
            calendar_week: 캘린더 주차

        Returns:
            tuple: (성공 수, 실패 수)
        """
        BATCH_SIZE = 20
        total_success = 0
        total_fail = 0
        crawl_results = []

        for idx, (keyword, search_url, _, _) in enumerate(keywords, 1):
            print(f"\n[{idx}/{len(keywords)}] ", end="")

            result = self.crawl_keyword(keyword, search_url)

            if result['success']:
                crawl_results.append({
                    'keyword': keyword,
                    'article_count': result['article_count'],
                    'crawl_datetime': result['crawl_datetime']
                })
            else:
                total_fail += 1

            # 배치 저장 (20개 도달 시)
            if len(crawl_results) >= BATCH_SIZE:
                success, fail = self.save_batch(crawl_results, calendar_week)
                total_success += success
                total_fail += fail
                crawl_results = []

            # 요청 간격
            if idx < len(keywords):
                self.browser.wait_random(2, 4)

        # 남은 결과 저장
        if crawl_results:
            success, fail = self.save_batch(crawl_results, calendar_week)
            total_success += success
            total_fail += fail

        return total_success, total_fail

    def save_batch(self, results, calendar_week):
        """배치 저장 (각 키워드별 crawl_datetime 사용)"""
        print_log("INFO", f"배치 저장 ({len(results)}건)")
        return self.db.save_batch_with_retry(results, calendar_week)

    def print_summary(self, success_count, fail_count, total_count):
        """결과 요약 출력"""
        mode_str = "테스트 모드" if self.test_mode else "운영 모드"
        print("\n" + "=" * 60)
        print("크롤링 완료")
        print("=" * 60)
        print(f"모드: {mode_str}")
        print(f"테이블: {self.db.table_name}")
        print(f"성공: {success_count}건")
        print(f"실패: {fail_count}건")
        print(f"총계: {total_count}건")

    def run(self):
        """메인 실행"""
        # 로거 초기화
        log_file = setup_logger()
        cleanup_old_logs()

        mode_str = "테스트 모드" if self.test_mode else "운영 모드"

        print("\n" + "=" * 60)
        print(f"Bing News 마켓 트렌드 크롤러 ({mode_str})")
        print(f"저장 테이블: {self.db.table_name}")
        print(f"로그 파일: {log_file}")
        print("=" * 60)

        try:
            if not self.setup():
                return

            # 테스트 모드: 필터 적용, 운영 모드: 전체 조회
            if self.test_mode:
                keywords = self.db.get_keywords(
                    product_line=self.test_product_line,
                    content_type=self.test_content_type,
                    limit=self.test_count
                )
                filter_info = []
                if self.test_product_line:
                    filter_info.append(f"product_line={self.test_product_line}")
                if self.test_content_type:
                    filter_info.append(f"content_type={self.test_content_type}")
                if self.test_count:
                    filter_info.append(f"limit={self.test_count}")
                if filter_info:
                    print_log("INFO", f"테스트 필터: {', '.join(filter_info)}")
            else:
                keywords = self.db.get_keywords()

            if not keywords:
                print_log("INFO", "처리할 키워드가 없습니다.")
                return

            print_log("INFO", f"{len(keywords)}개 키워드 조회됨")

            calendar_week = self.generate_calendar_week()
            success, fail = self.crawl_all_keywords(keywords, calendar_week)
            self.print_summary(success, fail, len(keywords))

        except Exception as e:
            print_log("ERROR", f"실행 오류: {e}")
            traceback.print_exc()

        finally:
            self.cleanup()


# ============================================================================
# 메인
# ============================================================================

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Bing News 마켓 트렌드 크롤러")
    print("=" * 60)
    print("\n[모드 선택]")
    print("  - 't' 입력: 테스트 모드 (test_market_trend 테이블)")
    print("  - 10초 내 입력 없음: 운영 모드 (market_trend 테이블)")
    print()

    # 10초 타임아웃으로 입력 받기
    user_input = get_input_with_timeout("모드 선택 (t=테스트, 10초 후 자동 운영모드): ", timeout=10)

    if user_input and user_input.lower().strip() == 't':
        test_mode = True
        print_log("INFO", "테스트 모드로 실행합니다.")

        # 테스트 모드 필터 입력
        print("\n[테스트 필터 설정] (엔터: 전체)")
        test_product_line = input("  product_line(TV/HHP): ").strip() or None
        test_content_type = input("  content_type(News/Event): ").strip() or None
        test_count_input = input("  test_count: ").strip()
        test_count = int(test_count_input) if test_count_input else None

        crawler = BingNewsCrawler(
            headless=False,
            test_mode=test_mode,
            test_product_line=test_product_line,
            test_content_type=test_content_type,
            test_count=test_count
        )
    else:
        test_mode = False
        print_log("INFO", "운영 모드로 실행합니다.")
        crawler = BingNewsCrawler(headless=False, test_mode=test_mode)

    crawler.run()
    if test_mode:
        input("\n엔터키를 누르면 종료합니다...")
