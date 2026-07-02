"""
BaseCrawler - HHP 크롤러 공통 기능 제공
모든 개별 크롤러(Main, BSR, Promotion, Detail)가 상속받는 베이스 클래스

섹션 구성:
1. 초기화: __init__
2. 디비: connect_db, load_xpaths, load_page_urls, is_product_excluded
3. 로깅: start_logging, stop_logging, cleanup_old_logs
4. 캡쳐: screenshot_with_watermark, take_capture, capture_page_with_scroll, cleanup_old_capture_folders
5. 봇/리트라이: add_random_mouse_movements, retry_on_network_error
6. 브라우저 헬퍼: get_chrome_version
7. 추출 헬퍼: extract_text_safe, extract_with_fallback, safe_extract, safe_extract_chain,
   safe_extract_chain_list, get_chain_xpaths, safe_extract_chain_join, safe_extract_join,
   generate_batch_id, generate_calendar_week
8. 정리 대상: setup_driver, setup_driver_stealth, set_amazon_zip_code,
   check_product_exists, update_product_rank, save_cookies, load_cookies
"""

import psycopg2
import time
import random
import re
import glob
import os
import sys
import pickle
import traceback
import gc
import shutil
import subprocess
from datetime import datetime, timedelta
import pytz
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from lxml import html
from PIL import Image, ImageDraw, ImageFont

from config import DB_CONFIG


class TeeLogger:
    """
    stdout/stderr를 콘솔과 파일 양쪽에 출력하는 클래스
    통합 크롤러에서 모든 print() 출력과 traceback을 로그 파일에 저장
    """

    def __init__(self, log_file_path):
        self.terminal_stdout = sys.stdout
        self.terminal_stderr = sys.stderr
        self.log_file = open(log_file_path, 'w', encoding='utf-8')

    def write(self, message):
        self.terminal_stdout.write(message)
        self.log_file.write(message)
        self.log_file.flush()

    def flush(self):
        self.terminal_stdout.flush()
        self.log_file.flush()

    def close(self):
        self.log_file.close()


class TeeLoggerStderr:
    """
    stderr를 콘솔과 파일 양쪽에 출력하는 클래스
    traceback.print_exc() 등 stderr 출력을 로그 파일에 저장
    """

    def __init__(self, tee_logger):
        self.tee_logger = tee_logger
        self.terminal_stderr = sys.stderr

    def write(self, message):
        self.terminal_stderr.write(message)
        self.tee_logger.log_file.write(message)
        self.tee_logger.log_file.flush()

    def flush(self):
        self.terminal_stderr.flush()
        self.tee_logger.log_file.flush()


class BaseCrawler:
    """
    HHP 크롤러 베이스 클래스
    공통 메서드를 제공하여 코드 중복 방지 및 유지보수성 향상
    """

    # ====================================================================
    # Initialization
    # ====================================================================
    def __init__(self):
        """초기화"""
        self.driver = None
        self.db_conn = None
        self.xpaths = {}
        self.tee_logger = None
        self.tee_logger_stderr = None
        self.original_stdout = None
        self.original_stderr = None

    # ====================================================================
    # DB
    # ====================================================================
    def connect_db(self, max_retries=3, base_delay=5):
        """
        PostgreSQL 데이터베이스 연결

        쓰임새:
        - 크롤러 시작 시 DB 연결 설정
        - config.py의 DB_CONFIG 정보 사용
        - 트랜잭션 모드로 동작 (commit/rollback 지원)

        Returns:
            bool: 연결 성공 시 True, 실패 시 False
        """
        for attempt in range(1, max_retries + 1):
            try:
                self.db_conn = psycopg2.connect(**DB_CONFIG, database='postgres')
                print("[SUCCESS] Database connected")
                return True
            except Exception as e:
                if self.db_conn:
                    try:
                        self.db_conn.close()
                    except Exception:
                        pass
                    self.db_conn = None

                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"[WARNING] Database connection failed (attempt {attempt}/{max_retries}): {e}")
                    print(f"[INFO] Retrying DB connection in {delay} seconds...")
                    time.sleep(delay)
                    continue

                print(f"[ERROR] Database connection failed after {max_retries} attempts: {e}")
                traceback.print_exc()
                return False

    def ensure_db_connection(self):
        """긴 크롤링 도중 PostgreSQL(RDS)이 idle/장시간 연결을 끊었으면 재연결한다.

        RDS는 오래 살아있는(또는 유휴) 연결을 서버에서 끊는데, psycopg2는 다음 쿼리를
        시도하기 전까지 conn.closed가 0으로 남아있을 수 있다. 따라서 직전 실패로 conn이
        닫힌 뒤 이 메서드를 호출하면 재연결되어 이후 제품 처리가 이어진다.
        """
        try:
            if self.db_conn and not self.db_conn.closed:
                return True
        except Exception:
            pass
        print("[WARNING] DB connection closed; reconnecting...")
        return self.connect_db()

    def safe_rollback(self):
        """연결이 이미 죽은 상태에서 rollback()이 InterfaceError(2차 예외)를 던지는 것을 방지."""
        try:
            if self.db_conn and not self.db_conn.closed:
                self.db_conn.rollback()
        except Exception:
            pass

    @staticmethod
    def fetch_today_batch_ids(table_name, account_name, test_mode=False):
        """오늘 날짜의 batch_id 목록 조회.

        test_mode=True면 table_name 앞에 test_ prefix를 붙인다.
        batch_id는 account_name과 오늘 날짜(YYYYMMDD)로 조회한다.
        """
        today_str = datetime.now().strftime('%Y%m%d')
        table = f"test_{table_name}" if test_mode else table_name
        batch_pattern = f"%{today_str}%"

        try:
            conn = psycopg2.connect(**DB_CONFIG, database='postgres')
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT DISTINCT batch_id FROM {table} "
                "WHERE account_name = %s AND batch_id LIKE %s "
                "ORDER BY batch_id DESC",
                (account_name, batch_pattern)
            )
            batch_ids = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
            return batch_ids
        except Exception as e:
            print(f"[WARN] batch_id 조회 실패: {e}")
            return []

    def is_product_excluded(self, item, item_mst_table=None):
        """item_mst에서 is_product=False인지 확인한다.

        item_mst_table 인자가 없으면 self.item_mst_table 값을 사용한다.
        조회 결과가 없으면 신규 item으로 보고 제외하지 않는다.
        """
        if not item:
            return False

        table_name = item_mst_table or getattr(self, 'item_mst_table', None)
        if not table_name:
            print("[WARNING] is_product_excluded skipped: item_mst_table not set")
            return False

        try:
            cursor = self.db_conn.cursor()
            cursor.execute(
                f"""
                SELECT is_product FROM {table_name}
                WHERE item = %s AND account_name = %s
                """,
                (item, self.account_name),
            )
            row = cursor.fetchone()
            cursor.close()

            if row is None:
                return False
            return row[0] is False
        except Exception:
            return False

    def find_excluded_keyword(self, retailer_sku_name, keywords=None):
        """retailer_sku_name에 포함된 제외 키워드를 반환한다."""
        keywords = keywords if keywords is not None else getattr(self, 'excluded_keywords', None)
        if not keywords:
            return None

        sku_name_lower = (retailer_sku_name or '').lower()
        for keyword in keywords:
            pattern = rf"(?:^|[^a-z0-9]){re.escape(keyword.lower())}(?:[^a-z0-9]|$)"
            if re.search(pattern, sku_name_lower):
                return keyword
        return None

    def get_chrome_version(self):
        """설치된 Chrome 브라우저의 메이저 버전을 감지한다. Windows 레지스트리 기반."""
        try:
            result = subprocess.run(
                ['reg', 'query', 'HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon', '/v', 'version'],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                match = re.search(r'(\d+)\.\d+\.\d+\.\d+', result.stdout)
                if match:
                    version = int(match.group(1))
                    print(f"[INFO] 감지된 Chrome 버전: {version}")
                    return version
        except Exception as e:
            print(f"[WARNING] Chrome 버전 감지 실패: {e}")
        return None

    def load_xpaths(self, account_name, page_type, corp, product_line):
        """
        dx_xpath_selectors 테이블에서 XPath 셀렉터 조회 (법인/상품군 인자로 분기)

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            page_type (str): 페이지 타입 (main, bsr, promotion, detail)
            corp (str): 법인 코드 (예: 'SEA')
            product_line (str): 상품군 (예: 'HHP', 'TV')

        Returns:
            bool: 로드 성공 시 True, 실패 시 False
        """
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT data_field, xpath, previous_xpath
                FROM dx_xpath_selectors
                WHERE corp = %s AND product_line = %s
                  AND account_name = %s AND page_type = %s AND is_active = TRUE
            """, (corp, product_line, account_name, page_type))

            for row in cursor.fetchall():
                self.xpaths[row[0]] = {
                    'xpath': row[1],
                    'previous_xpath': row[2]
                }

            cursor.close()
            print(f"[SUCCESS] Loaded {len(self.xpaths)} XPath selectors for {corp}/{product_line}/{account_name}/{page_type}")
            return True

        except Exception as e:
            print(f"[ERROR] Failed to load XPaths: {e}")
            traceback.print_exc()
            return False

    def load_page_urls(self, account_name, page_type, corp, product_line):
        """
        dx_target_page_url 테이블에서 크롤링 대상 URL 템플릿 조회 (법인/상품군 인자로 분기)

        쓰임새:
        - Main/BSR/Promotion 크롤러가 크롤링할 URL 템플릿을 가져옴
        - URL 템플릿의 {page} 플레이스홀더를 페이지 번호로 치환하여 사용

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            page_type (str): 페이지 타입 (main, bsr, promotion)
            corp (str): 법인 코드 (예: 'SEA')
            product_line (str): 상품군 (예: 'HHP', 'TV')

        Returns:
            str or None: URL 템플릿 문자열, 실패 시 None
        """
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                SELECT url_template
                FROM dx_target_page_url
                WHERE corp = %s AND product_line = %s
                  AND account_name = %s AND page_type = %s
            """, (corp, product_line, account_name, page_type))

            result = cursor.fetchone()
            cursor.close()

            if result:
                print(f"[SUCCESS] Loaded URL template for {corp}/{product_line}/{account_name}/{page_type}")
                return result[0]
            else:
                print(f"[WARNING] No URL template found for {corp}/{product_line}/{account_name}/{page_type}")
                return None

        except Exception as e:
            print(f"[ERROR] Failed to load page URLs: {e}")
            traceback.print_exc()
            return None

    # ====================================================================
    # Logging
    # ====================================================================
    def start_logging(self, batch_id):
        """
        콘솔 출력을 파일에도 저장하기 시작

        쓰임새:
        - 통합 크롤러 시작 시 호출
        - 이후 모든 print() 출력이 콘솔과 파일 양쪽에 기록됨

        Args:
            batch_id (str): 배치 ID

        Returns:
            str: 로그 파일 경로
        """
        try:
            # logs 폴더 경로 (프로젝트 루트/logs/)
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logs_dir = os.path.join(project_root, 'logs')
            os.makedirs(logs_dir, exist_ok=True)

            # 로그 파일명: {batch_id}_{생성시간}.txt
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            log_file_path = os.path.join(logs_dir, f"{batch_id}_{timestamp}.txt")

            # TeeLogger 시작 (stdout + stderr)
            self.original_stdout = sys.stdout
            self.original_stderr = sys.stderr
            self.tee_logger = TeeLogger(log_file_path)
            self.tee_logger_stderr = TeeLoggerStderr(self.tee_logger)
            sys.stdout = self.tee_logger
            sys.stderr = self.tee_logger_stderr

            return log_file_path

        except Exception as e:
            print(f"[WARNING] Failed to start logging: {e}")
            return None

    def stop_logging(self):
        """
        콘솔 출력 파일 저장 종료

        쓰임새:
        - 통합 크롤러 종료 시 호출
        - stdout/stderr를 원래대로 복원하고 로그 파일 닫기
        """
        try:
            if self.tee_logger and self.original_stdout:
                sys.stdout = self.original_stdout
                sys.stderr = self.original_stderr
                self.tee_logger.close()
                self.tee_logger = None
                self.tee_logger_stderr = None
                self.original_stdout = None
        except Exception as e:
            print(f"[WARNING] Failed to stop logging: {e}")

    def cleanup_old_logs(self, days=30):
        """
        오래된 로그 파일 자동 삭제

        쓰임새:
        - 크롤러 시작 시 호출하여 오래된 로그 자동 정리
        - 디스크 공간 절약
        - 기본 30일 이상 된 로그 파일 삭제

        Args:
            days (int): 보관 일수 (기본: 30일)

        Returns:
            None
        """
        try:
            # logs 폴더 경로 (프로젝트 루트/logs/)
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            logs_dir = os.path.join(project_root, 'logs')

            if not os.path.exists(logs_dir):
                return

            # 삭제 기준 시간 계산
            cutoff_time = datetime.now() - timedelta(days=days)

            # *.txt 패턴 파일 검색 (logs/ 하단의 모든 txt 파일)
            log_pattern = os.path.join(logs_dir, '*.txt')
            log_files = glob.glob(log_pattern)

            deleted_count = 0
            for log_file in log_files:
                # 파일 수정 시간 확인
                file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))

                # 기준 시간보다 오래된 파일 삭제
                if file_mtime < cutoff_time:
                    os.remove(log_file)
                    deleted_count += 1

            if deleted_count > 0:
                print(f"[INFO] Deleted {deleted_count} old log files (older than {days} days)")

        except Exception as e:
            print(f"[WARNING] Failed to cleanup old logs: {e}")

    # ====================================================================
    # Capture
    # ====================================================================
    def screenshot_with_watermark(self, filepath, quality=85):
        """현재 화면을 캡처하고 URL/시간 워터마크를 붙여 JPG로 저장한다."""
        temp_filepath = filepath.replace('.jpg', '_temp.png')

        self.page.get_screenshot(path=temp_filepath)

        img = Image.open(temp_filepath)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        current_url = self.page.url if self.page else ''

        try:
            font = ImageFont.truetype("arial.ttf", 48)
        except Exception as e:
            print(f"  [INFO] arial.ttf 로드 실패 → 기본 폰트 사용: {e}")
            font = ImageFont.load_default()

        padding = 16

        if current_url:
            url_bbox = ImageDraw.Draw(img).textbbox((0, 0), current_url, font=font)
            header_height = url_bbox[3] - url_bbox[1] + padding * 2
            new_img = Image.new('RGB', (img.width, img.height + header_height), 'white')
            new_img.paste(img, (0, header_height))
            img = new_img
            draw = ImageDraw.Draw(img)
            draw.text((padding, padding), current_url, fill='black', font=font)
        else:
            draw = ImageDraw.Draw(img)

        ts_bbox = draw.textbbox((0, 0), timestamp, font=font)
        footer_height = ts_bbox[3] - ts_bbox[1] + padding * 2
        footer_img = Image.new('RGB', (img.width, img.height + footer_height), 'white')
        footer_img.paste(img, (0, 0))
        img = footer_img
        draw = ImageDraw.Draw(img)
        ts_x = img.width - (ts_bbox[2] - ts_bbox[0]) - padding
        ts_y = img.height - footer_height + padding
        draw.text((ts_x, ts_y), timestamp, fill='black', font=font)

        img = img.convert('RGB')
        img.save(filepath, 'JPEG', quality=quality)
        del img
        gc.collect()

        if os.path.exists(temp_filepath):
            os.remove(temp_filepath)

    def should_take_capture(self, page_type):
        """Return whether detail capture is still allowed for the source page type."""
        if not getattr(self, 'capture_enabled', False):
            return False

        if getattr(self, 'capture_all', False):
            return True

        if page_type == 'main':
            return getattr(self, 'capture_main_count', 0) < getattr(self, 'capture_limit_per_type', 0)
        if page_type == 'bsr':
            return getattr(self, 'capture_bsr_count', 0) < getattr(self, 'capture_limit_per_type', 0)
        return False

    def mark_capture_count(self, page_type):
        """Increment the capture counter for the source page type."""
        if getattr(self, 'capture_all', False):
            return

        if page_type == 'main':
            self.capture_main_count += 1
        elif page_type == 'bsr':
            self.capture_bsr_count += 1

    def take_capture(self, item, step):
        """Detail 화면을 1장 캡처한다."""
        if not getattr(self, 'capture_enabled', True):
            return False

        capture_dir = os.path.join(self.capture_base_dir, self.batch_id or 'unknown')
        os.makedirs(capture_dir, exist_ok=True)
        filename = f"dt_{item}_{self.batch_id}_{step}.jpg"
        filepath = os.path.join(capture_dir, filename)

        try:
            self.screenshot_with_watermark(filepath)
            print(f"  [캡처] {filename} 저장 완료")
            return True
        except Exception as e:
            print(f"  [캡처] {filename} 저장 실패: {e}")
            return False

    def capture_page_with_scroll(self):
        """스크롤하면서 전체 화면을 캡처한다. 파일명 prefix는 self.page_type을 사용한다."""
        try:
            # 오래된 캡처 폴더 삭제
            self.cleanup_old_capture_folders(days=2)

            # 캡처 폴더: {capture_base_dir}/{batch_id}/
            capture_dir = os.path.join(self.capture_base_dir, self.batch_id)
            os.makedirs(capture_dir, exist_ok=True)

            # 페이지 상단으로 이동
            self.page.run_js("window.scrollTo(0, 0);")
            time.sleep(1)

            capture_num = 1
            last_height = 0

            while True:
                # 캡처 파일명: {page_type}_{batch_id}_{N}.jpg
                filename = f"{self.page_type}_{self.batch_id}_{capture_num}.jpg"
                filepath = os.path.join(capture_dir, filename)
                self.screenshot_with_watermark(filepath)
                print(f"  [캡처] {filename} 저장 완료")

                # 스크롤
                self.page.run_js("window.scrollBy(0, 800)")
                time.sleep(1)

                # 스크롤 끝 체크
                new_height = self.page.run_js("return window.pageYOffset")
                if new_height == last_height:
                    break
                last_height = new_height
                capture_num += 1

            print(f"  [캡처] 총 {capture_num}개 캡처 완료")
        except Exception as e:
            print(f"  [캡처] 실패: {e}")
            traceback.print_exc()

    def cleanup_old_capture_folders(self, days=2):
        """오래된 캡처 폴더를 삭제한다. 기본값은 2일 이상 지난 폴더다."""
        try:
            if not os.path.exists(self.capture_base_dir):
                return

            today = datetime.now().date()
            deleted_count = 0

            for folder_name in os.listdir(self.capture_base_dir):
                folder_path = os.path.join(self.capture_base_dir, folder_name)
                if not os.path.isdir(folder_path):
                    continue

                # 폴더명에서 날짜 추출 (예: a_20260128_100000 -> 20260128)
                try:
                    date_part = folder_name.split('_')[1]
                    folder_date = datetime.strptime(date_part, '%Y%m%d').date()

                    if (today - folder_date).days >= days:
                        shutil.rmtree(folder_path)
                        deleted_count += 1
                        print(f"  [캡처] 오래된 폴더 삭제: {folder_name}")
                except (IndexError, ValueError):
                    continue

            if deleted_count > 0:
                print(f"  [캡처] 총 {deleted_count}개 폴더 삭제 완료")
        except Exception as e:
            print(f"  [캡처] 폴더 정리 실패: {e}")

    # ====================================================================
    # Bot / Retry
    # ====================================================================
    def add_random_mouse_movements(self):
        """사람처럼 보이기 위한 랜덤 마우스 움직임."""
        try:
            if not getattr(self, 'page', None):
                return
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                self.page.actions.move_to((x, y))
                time.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            print(f"[ERROR] 마우스 움직임 실패: {e}")
            traceback.print_exc()

    def retry_on_network_error(self, func, max_retries=3, delay=5):
        """
        네트워크 에러 발생 시 재시도 데코레이터

        쓰임새:
        - 네트워크 불안정으로 인한 일시적 오류 대응
        - 최대 3회까지 재시도 (재시도 간격 5초)
        - TimeoutException, ConnectionError 등에 대응

        Args:
            func: 재시도할 함수
            max_retries (int): 최대 재시도 횟수 (기본: 3)
            delay (int): 재시도 간격 초 (기본: 5초)

        Returns:
            function result or None: 함수 실행 결과 또는 실패 시 None
        """
        for attempt in range(max_retries):
            try:
                return func()
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[WARNING] Attempt {attempt + 1} failed: {e}")
                    print(f"[INFO] Retrying in {delay} seconds...")
                    time.sleep(delay)
                else:
                    print(f"[ERROR] All {max_retries} attempts failed")
                    return None

    def normalize_redirect_product_name(self, product_name):
        if product_name is None:
            return ''
        return ' '.join(str(product_name).split()).casefold()

    def extract_first_text_from_tree(self, tree, xpaths):
        if tree is None:
            return None

        for xpath in xpaths:
            try:
                values = tree.xpath(xpath)
            except Exception:
                continue

            text_parts = []
            for value in values:
                if hasattr(value, 'text_content'):
                    text_parts.append(value.text_content())
                else:
                    text_parts.append(str(value))

            normalized = self.normalize_redirect_product_name(' '.join(text_parts))
            if normalized:
                return normalized

        return None

    def redirect_product_names_match(self, listing_name, detail_name):
        listing_normalized = self.normalize_redirect_product_name(listing_name)
        detail_normalized = self.normalize_redirect_product_name(detail_name)
        return bool(listing_normalized and detail_normalized and listing_normalized == detail_normalized)

    def ensure_listing_item(self, product):
        if product is None:
            return None
        if not product.get('item'):
            product['item'] = self.extract_item(product.get('product_url'))
        return product

    # ====================================================================
    # Extract Helper
    # ====================================================================
    def extract_text_safe(self, element, xpath):
        """
        XPath를 사용하여 안전하게 텍스트 추출

        쓰임새:
        - lxml element에서 XPath로 데이터 추출 시 에러 방지
        - 속성 추출 (예: @href)과 텍스트 추출 모두 지원
        - 값이 없거나 에러 발생 시 None 반환

        Args:
            element: lxml HTML element
            xpath (str): XPath 표현식

        Returns:
            str or None: 추출된 텍스트, 실패 시 None
        """
        try:
            result = element.xpath(xpath)
            if result:
                # 속성 추출인 경우 (예: @href)
                if isinstance(result[0], str):
                    return result[0].strip()
                # 요소 텍스트 추출인 경우
                else:
                    return result[0].text_content().strip()
            return None
        except Exception:
            return None

    def extract_with_fallback(self, element, xpath, default=None):
        """
        파싱 실패 시 기본값을 반환하는 안전한 추출

        쓰임새:
        - extract_text_safe의 래퍼 함수
        - 추출 실패 시 사용자 지정 기본값 반환
        - NULL 값 대신 특정 문자열을 저장하고 싶을 때 사용

        Args:
            element: lxml HTML element
            xpath (str): XPath 표현식
            default: 추출 실패 시 반환할 기본값

        Returns:
            str or default: 추출된 텍스트 또는 기본값
        """
        result = self.extract_text_safe(element, xpath)
        return result if result is not None else default

    def safe_extract(self, element, field_name):
        """필드 추출 시 예외 발생하면 None 반환 후 다음 필드로 진행"""
        try:
            return self.extract_with_fallback(element, self.xpaths.get(field_name, {}).get('xpath'))
        except Exception as e:
            print(f"[WARNING] Failed to extract {field_name}: {e}")
            return None

    def safe_extract_chain(self, element, base_field_name):
        """
        base_field_name과 관련된 모든 xpath를 순서대로 시도하여 첫 번째 성공값 반환

        쓰임새:
        - A/B 테스트 등으로 페이지 구조가 다를 때 fallback xpath 자동 시도
        - DB에 같은 필드의 fallback 변형을 등록해두면 코드 수정 없이 대응 가능

        탐색 순서:
        1. base_field_name 정확히 일치하는 xpath 먼저 시도
        2. base_field_name_fallback 으로 시작하는 xpath만 이름순으로 시도
           예: sku_fallback, sku_fallback2는 대상이고 sku_v2는 대상이 아님

        예시:
            self.safe_extract_chain(tree, 'recommendation_intent')
            → recommendation_intent → recommendation_intent_fallback → recommendation_intent_fallback2 순서로 시도

        Args:
            element: lxml HTML element
            base_field_name (str): 기본 필드명

        Returns:
            str or None: 첫 번째 추출 성공값, 전부 실패 시 None
        """
        # 1) base xpath 먼저 시도 (로그 없음 — 기본 매칭은 정상이므로)
        result = self.safe_extract(element, base_field_name)
        if result:
            return result

        # 2) fallback xpath 시도: base_fallback* 형식만 chain 대상으로 본다.
        #    base_v2처럼 fallback으로 시작하지 않는 변형은 의도적으로 제외한다.
        related = sorted([
            key for key in self.xpaths.keys()
            if key.startswith(base_field_name + '_fallback')
        ])

        for field_name in related:
            result = self.safe_extract(element, field_name)
            if result:
                print(f"  [chain] {base_field_name} → matched: {field_name} (fallback)")
                return result

        return None

    def extract_product_url(self, item, base_domain):
        """상품 컨테이너에서 product_url을 추출하고 상대 경로면 base_domain을 붙인다."""
        product_url = self.safe_extract_chain(item, 'product_url')
        if product_url == '#':
            return None
        if product_url and product_url.startswith('/'):
            return f"{base_domain}{product_url}"
        return product_url

    def safe_extract_chain_list(self, element, base_field_name):
        """
        base_field_name과 관련된 모든 xpath를 순서대로 시도하여 
        첫 번째로 매칭된 대상의 전체 '결과 요소 리스트' 자체를 그대로 반환합니다.
        
        쓰임새:
        - 상세 리뷰 추출과 같이 결과가 여러 개이며, 이들을 리스트(배열)로 순회해야 할 때 사용.
        - 매번 for문을 직접 작성할 필요 없이, 이 메서드 하나로 처리가 가능.
        
        Returns:
            list: 추출된 요소들의 리스트 (실패 시 빈 리스트 [] 반환)
            str: 매칭에 성공한 최종 사용 XPath 변수명 (실패 시 None)
        """
        # 1. 기본 xpath 먼저 시도
        base_xpath = self.xpaths.get(base_field_name, {}).get('xpath')
        if base_xpath:
            try:
                results = element.xpath(base_xpath)
                if results:
                    return results, base_field_name
            except Exception as e:
                pass

        # 2. fallback xpath 들을 이름 순으로 정렬하여 시도
        #    base_fallback* 형식만 대상이며, base_v2 같은 변형은 제외한다.
        related = sorted([
            key for key in self.xpaths.keys()
            if key.startswith(base_field_name + '_fallback')
        ])

        for field_name in related:
            xpath = self.xpaths.get(field_name, {}).get('xpath')
            if xpath:
                try:
                    results = element.xpath(xpath)
                    if results:
                        print(f"  [chain_list] {base_field_name} → matched: {field_name}")
                        return results, field_name
                except Exception as e:
                    pass

        return [], None

    def get_chain_xpaths(self, base_field_name):
        """
        base_field_name과 fallback xpath를 순서대로 반환 (이름, xpath) 튜플 리스트
        직접 xpath를 실행해야 하는 경우 사용 (리스트 추출 등)
        fallback 대상은 base_fallback* 형식만 포함하며, base_v2 같은 변형은 제외한다.
        """
        result = []
        base_xpath = self.xpaths.get(base_field_name, {}).get('xpath')
        if base_xpath:
            result.append((base_field_name, base_xpath))

        related = sorted([
            key for key in self.xpaths.keys()
            if key.startswith(base_field_name + '_fallback')
        ])
        for field_name in related:
            xpath = self.xpaths.get(field_name, {}).get('xpath')
            if xpath:
                result.append((field_name, xpath))

        return result

    def safe_extract_chain_join(self, element, base_field_name, separator=" / "):
        """
        safe_extract_chain의 join 버전 — 여러 요소를 결합하여 반환

        탐색 순서는 safe_extract_chain과 동일

        Args:
            element: lxml HTML element
            base_field_name (str): 기본 필드명
            separator (str): 결합 구분자

        Returns:
            str or None: 첫 번째 추출 성공값 (결합), 전부 실패 시 None
        """
        result = self.safe_extract_join(element, base_field_name, separator)
        if result:
            return result

        related = sorted([
            key for key in self.xpaths.keys()
            if key.startswith(base_field_name + '_fallback')
        ])

        for field_name in related:
            result = self.safe_extract_join(element, field_name, separator)
            if result:
                print(f"  [chain] {base_field_name} → matched: {field_name}")
                return result

        return None

    def scroll_extract(self, tree, extractors, max_scrolls=2, scroll_px=(200, 300), wait_sec=0.3):
        """
        현재 tree에서 값을 추출하고, 실패한 필드만 스크롤하며 재추출한다.

        extractors:
        - callable: 단일 값 추출
        - {name: callable}: 여러 값을 추출

        여러 name을 전달하면 모든 name이 추출되거나 max_scrolls에 도달할 때까지 진행한다.
        이미 추출된 name은 다음 스크롤 이후 다시 추출하지 않는다.
        """
        if callable(extractors):
            extractors = {'value': extractors}
            single_value = True
        else:
            single_value = False
            extractors = dict(extractors)

        results = {name: None for name in extractors}
        pending = set(extractors.keys())

        def try_extract(current_tree):
            for name in list(pending):
                value = extractors[name](current_tree)
                if value:
                    results[name] = value
                    pending.remove(name)

        try_extract(tree)

        scroll_min, scroll_max = scroll_px
        for _ in range(max_scrolls):
            if not pending:
                break

            self.page.scroll.down(random.randint(scroll_min, scroll_max))
            time.sleep(wait_sec)
            tree = html.fromstring(self.page.html)
            try_extract(tree)

        if single_value:
            return results['value'], tree
        return results, tree

    def scroll_find_element(self, xpath_input, max_scrolls, label, click=False, return_element=False, scroll_px=(300, 400)):
        """DOM에서 먼저 찾고, 없으면 스크롤하면서 요소를 찾는다."""
        scroll_min, scroll_max = scroll_px

        if isinstance(xpath_input, str):
            xpath = self.xpaths.get(xpath_input, {}).get('xpath')
            if not xpath:
                print(f"  [WARNING] DB에 '{xpath_input}' XPath 미등록 → {label} 스킵")
                return (None, None) if return_element else False
            candidates = [(xpath_input, xpath)]
        else:
            candidates = list(xpath_input)
            if not candidates:
                print(f"  [WARNING] chain xpath 비어있음 → {label} 스킵")
                return (None, None) if return_element else False

        do_click = click and not return_element
        click_suffix = " + 클릭" if do_click else ""
        is_chain = len(candidates) > 1
        last_error = None

        for xpath_name, xpath in candidates:
            try:
                elem = self.page.ele(f'xpath:{xpath}', timeout=2)
                if elem:
                    elem.scroll.to_see()
                    time.sleep(0.3)
                    if do_click:
                        elem.click()
                    name_suffix = f" [{xpath_name}]" if is_chain else ""
                    print(f"  [{label}] [OK] DOM에서 즉시 발견{name_suffix}{click_suffix}")
                    return (elem, xpath_name) if return_element else True
            except Exception as e:
                last_error = e

        print(f"  [{label}] DOM 미발견 → 스크롤 탐색 시작")

        for scroll_count in range(1, max_scrolls + 1):
            pos_before = self.page.run_js("return window.pageYOffset")
            self.page.scroll.down(random.randint(scroll_min, scroll_max))
            time.sleep(0.3)
            pos_after = self.page.run_js("return window.pageYOffset")

            for xpath_name, xpath in candidates:
                try:
                    elem = self.page.ele(f'xpath:{xpath}', timeout=1)
                    if elem:
                        elem.scroll.to_see()
                        time.sleep(0.3)
                        if do_click:
                            elem.click()
                        name_suffix = f" [{xpath_name}]" if is_chain else ""
                        print(f"  [{label}] [OK] 스크롤 {scroll_count}회 → 발견{name_suffix}{click_suffix}")
                        return (elem, xpath_name) if return_element else True
                except Exception as e:
                    last_error = e

            if pos_after == pos_before:
                print(f"  [{label}] 페이지 바닥 도달")
                return (None, None) if return_element else False

        print(f"  [{label}] [FAIL] 스크롤 {max_scrolls}회 후에도 못 찾음")
        if last_error:
            print(f"  [WARNING] {label} 중 마지막 예외: {last_error}")
        return (None, None) if return_element else False

    def safe_extract_join(self, element, field_name, separator=" / "):
        """
        여러 요소를 추출하여 구분자로 결합

        쓰임새:
        - Union XPath (|)로 여러 요소를 선택하는 경우
        - 예: shipping_info에서 primary + secondary delivery message를 결합
        - separator로 결합 구분자 지정 가능

        Args:
            element: lxml HTML element
            field_name (str): XPath 필드명 (xpaths 딕셔너리 키)
            separator (str): 요소 결합 구분자 (기본: " / ")

        Returns:
            str or None: 결합된 텍스트, 요소 없으면 None
        """
        try:
            xpath = self.xpaths.get(field_name, {}).get('xpath')
            if not xpath:
                return None

            elements = element.xpath(xpath)
            if not elements:
                return None

            # 각 요소에서 텍스트 추출
            texts = []
            for elem in elements:
                if isinstance(elem, str):
                    text = elem.strip()
                else:
                    text = elem.text_content().strip()
                if text:
                    texts.append(text)

            if not texts:
                return None

            # 요소가 1개면 그대로, 2개 이상이면 구분자로 결합
            return separator.join(texts) if len(texts) > 1 else texts[0]

        except Exception as e:
            print(f"[WARNING] Failed to extract_join {field_name}: {e}")
            return None

    def generate_batch_id(self, account_name, test_mode=False):
        """
        배치 ID 생성 (쇼핑몰 prefix + 타임스탬프)

        쓰임새:
        - 크롤링 세션을 구분하기 위한 고유 ID 생성
        - 같은 배치에서 수집된 데이터를 그룹화
        - 중복 방지 로직에서 사용 (batch_id + product_url 조합)

        형식:
        - 운영: a_20231120_143045
        - 테스트: t_a_20231120_143045

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            test_mode (bool): 테스트 모드 여부 (True면 t_ prefix 추가)

        Returns:
            str: 생성된 배치 ID
        """
        # 쇼핑몰별 prefix 매핑
        prefix_map = {
            'Amazon': 'a_',
            'Bestbuy': 'b_',
            'Walmart': 'w_'
        }

        prefix = prefix_map.get(account_name, 'x_')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        # 테스트 모드면 t_ prefix 추가
        if test_mode:
            return f"t_{prefix}{timestamp}"

        return f"{prefix}{timestamp}"

    def generate_calendar_week(self):
        """
        캘린더 주차 생성 (예: w47)

        쓰임새:
        - 데이터 수집 시점의 주차 정보 기록
        - 주별 데이터 분석 및 리포트에 사용
        - ISO 8601 주차 계산 방식 사용
        
        Returns:
            str: 캘린더 주차 (예: 'w47')
        """
        week_number = datetime.now().isocalendar()[1]  # ISO week number
        return f"w{week_number}"

    # ====================================================================
    # Cleanup Targets
    # Cleanup targets
    # ====================================================================
    # Cleanup target usage:
    # - base_crawler.py: called by setup_driver_stealth() for non-Amazon fallback
    # - amazon/tv/amazon_tv_item_mst.py: AmazonTVItemMstCrawler.initialize()
    # - amazon/hhp/amazon_hhp_item.py: AmazonItemCrawler.initialize()
    def setup_driver(self):
        """
        Chrome WebDriver 설정 및 초기화

        쓰임새:
        - Selenium을 사용한 동적 웹 크롤링을 위한 WebDriver 설정
        - 자동화 감지 방지 옵션 적용
        - User-Agent 설정으로 일반 브라우저처럼 동작
        - 일관된 결과를 위한 세션 및 쿠키 관리

        Returns:
            None
        """
        chrome_options = Options()

        # Page Load Strategy 설정 (동적 페이지 로딩 최적화)
        chrome_options.page_load_strategy = 'none'  # 전체 페이지 로드를 기다리지 않음

        # 자동화 감지 방지
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # User-Agent 고정 (일관된 결과를 위해)
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

        # 전체화면으로 시작
        chrome_options.add_argument('--start-maximized')

        # 추가 안정화 옵션
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--lang=ko-KR')  # 언어 고정

        # 쿠키 및 세션 유지를 위한 프로필 디렉토리 설정 (선택적)
        # chrome_options.add_argument('--user-data-dir=./chrome_profile')

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

        # 창 최대화
        self.driver.maximize_window()

        # 페이지 로드 타임아웃 설정 (120초)
        self.driver.set_page_load_timeout(120)

        # 자동화 감지 방지 스크립트 실행
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            '''
        })

        print("[SUCCESS] WebDriver setup complete")

    # Cleanup target usage:
    # - amazon/hhp/amazon_hhp_main.py: initialize(), restart_browser()
    # - amazon/hhp/amazon_hhp_bsr.py: initialize(), restart_browser()
    # - amazon/hhp/amazon_hhp_dt_update.py: initialize(), restart_driver()
    def setup_driver_stealth(self, account_name='Amazon'):
        """
        강화된 봇 감지 회피 Chrome WebDriver 설정

        쓰임새:
        - Main/BSR 크롤러에서 쿠키 없이 크롤링할 때 사용
        - Amazon의 봇 감지를 회피하기 위한 강화된 설정 적용
        - navigator.webdriver, plugins, WebGL 등 다양한 속성 위장

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)

        Returns:
            None
        """
        # Amazon만 강화된 봇 감지 회피 적용, 다른 쇼핑몰은 기본 setup_driver 사용
        if account_name != 'Amazon':
            self.setup_driver()
            return

        chrome_options = Options()

        # 기본 옵션
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')

        # 봇 감지 회피 옵션
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--allow-running-insecure-content')

        # 창 크기 설정 (봇처럼 보이지 않게)
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--start-maximized')

        # User-Agent 설정 (최신 Chrome 버전)
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

        # 자동화 흔적 숨기기
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # 언어 설정
        chrome_options.add_argument('--lang=en-US')
        prefs = {
            'intl.accept_languages': 'en-US,en',
            'credentials_enable_service': False,
            'profile.password_manager_enabled': False
        }
        chrome_options.add_experimental_option('prefs', prefs)

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

        # 창 최대화
        self.driver.maximize_window()

        # 페이지 로드 타임아웃 설정 (120초)
        self.driver.set_page_load_timeout(120)

        # CDP 명령으로 webdriver 속성 및 기타 자동화 흔적 숨기기
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                // webdriver 속성 숨기기
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // chrome 객체 정상화
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                // permissions 쿼리 오버라이드
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // plugins 배열 정상화 (빈 배열이면 봇으로 탐지됨)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                // languages 정상화
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // platform 정상화
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32'
                });

                // hardware concurrency (CPU 코어 수)
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8
                });

                // device memory
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8
                });

                // WebGL 벤더/렌더러 정상화
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    return getParameter.apply(this, arguments);
                };
            '''
        })

        # User-Agent 클라이언트 힌트 설정
        self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            "platform": "Windows",
            "acceptLanguage": "en-US,en;q=0.9"
        })

        print("[SUCCESS] WebDriver setup complete (stealth mode)")

        # Amazon인 경우 뉴욕 ZIP 코드 자동 설정
        if account_name == 'Amazon':
            self.set_amazon_zip_code('10001')

    # Cleanup target usage:
    # - base_crawler.py: called by setup_driver_stealth() for Amazon
    # - common/amazon_base.py has a separate DrissionPage implementation with the same name
    def set_amazon_zip_code(self, zip_code='10001', max_refresh=10):
        """
        Amazon 배송 지역 ZIP 코드 설정 (뉴욕: 10001)

        쓰임새:
        - 크롤러 시작 시 배송 지역을 뉴욕으로 고정
        - 일관된 가격/재고 정보 수집을 위해 필요

        Args:
            zip_code (str): ZIP 코드 (기본: 10001 - 맨해튼)
            max_refresh (int): 페이지 새로고침 최대 횟수 (기본: 10)

        Returns:
            bool: 설정 성공 시 True, 실패 시 False
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import random

        try:
            print(f"[INFO] Setting Amazon ZIP code to {zip_code}...")

            # Amazon 메인 페이지 접속
            self.driver.get('https://www.amazon.com')
            time.sleep(random.uniform(5, 8))

            # "Deliver to" 버튼 찾기 (못 찾으면 새로고침 3회)
            deliver_to_btn = None
            for attempt in range(1, max_refresh + 1):
                try:
                    deliver_to_btn = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.ID, 'nav-global-location-popover-link'))
                    )
                    break
                except Exception as e:
                    print(f"[WARNING] Deliver to button not found (attempt {attempt}/{max_refresh})")
                    if attempt < max_refresh:
                        print(f"[INFO] Refreshing page...")
                        self.driver.refresh()
                        time.sleep(random.uniform(5, 8))

            if not deliver_to_btn:
                raise Exception("Deliver to button not found after all refresh attempts")

            deliver_to_btn.click()
            time.sleep(random.uniform(2, 3))

            # ZIP 코드 입력 필드 찾기
            zip_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, 'GLUXZipUpdateInput'))
            )
            zip_input.clear()
            zip_input.send_keys(zip_code)
            time.sleep(random.uniform(1, 2))

            # Apply 버튼 클릭
            apply_btn = self.driver.find_element(By.CSS_SELECTOR, '#GLUXZipUpdate input[type="submit"], #GLUXZipUpdate .a-button-input')
            apply_btn.click()
            time.sleep(random.uniform(3, 5))

            # 팝업 닫기 (있는 경우)
            try:
                close_btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, '.a-popover-footer button, #GLUXConfirmClose'))
                )
                close_btn.click()
                time.sleep(1)
            except:
                pass  # 팝업이 없을 수 있음

            print(f"[SUCCESS] Amazon ZIP code set to {zip_code} (New York)")
            return True

        except Exception as e:
            print(f"[WARNING] Failed to set Amazon ZIP code: {e}")
            print(f"[INFO] Continuing without ZIP code setting...")
            return False

    # Cleanup target usage:
    # - No crawler call sites found
    # - Mentioned only in CLAUDE.md
    def check_product_exists(self, account_name, batch_id, product_url):
        """
        product_list 테이블에서 제품 존재 여부 확인

        쓰임새:
        - BSR/Promotion 크롤러에서 중복 확인에 사용
        - 같은 batch_id + product_url 조합이 이미 있는지 확인
        - 존재하면 UPDATE, 없으면 INSERT 결정

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            batch_id (str): 배치 ID
            product_url (str): 제품 URL

        Returns:
            bool: 제품이 존재하면 True, 없으면 False
        """
        try:
            # 테이블명 매핑
            table_map = {
                'Amazon': 'amazon_hhp_product_list',
                'Bestbuy': 'bby_hhp_product_list',
                'Walmart': 'wmart_hhp_product_list'
            }

            table_name = table_map.get(account_name)
            if not table_name:
                return False

            cursor = self.db_conn.cursor()
            cursor.execute(f"""
                SELECT COUNT(*) FROM {table_name}
                WHERE batch_id = %s AND product_url = %s
            """, (batch_id, product_url))

            count = cursor.fetchone()[0]
            cursor.close()

            return count > 0

        except Exception as e:
            print(f"[ERROR] Failed to check product existence: {e}")
            traceback.print_exc()
            return False

    # Cleanup target usage:
    # - No crawler call sites found
    # - Mentioned only in CLAUDE.md
    def update_product_rank(self, account_name, batch_id, product_url, rank_type, rank_value, additional_fields=None):
        """
        기존 제품에 rank 정보 업데이트

        쓰임새:
        - BSR/Promotion 크롤러에서 이미 존재하는 제품에 순위 정보 추가
        - Main에서 수집된 제품이 BSR에도 있을 때 bsr_rank 업데이트
        - 추가 필드도 함께 업데이트 가능

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            batch_id (str): 배치 ID
            product_url (str): 제품 URL
            rank_type (str): 순위 타입 (main_rank, bsr_rank, trend_rank)
            rank_value (int): 순위 값
            additional_fields (dict): 추가로 업데이트할 필드 딕셔너리

        Returns:
            bool: 업데이트 성공 시 True, 실패 시 False
        """
        try:
            # 테이블명 매핑
            table_map = {
                'Amazon': 'amazon_hhp_product_list',
                'Bestbuy': 'bby_hhp_product_list',
                'Walmart': 'wmart_hhp_product_list'
            }

            table_name = table_map.get(account_name)
            if not table_name:
                return False

            # UPDATE 쿼리 구성
            update_fields = [f"{rank_type} = %s"]
            values = [rank_value]

            # 추가 필드가 있으면 포함
            if additional_fields:
                for field, value in additional_fields.items():
                    update_fields.append(f"{field} = %s")
                    values.append(value)

            # WHERE 조건 값 추가
            values.extend([batch_id, product_url])

            query = f"""
                UPDATE {table_name}
                SET {', '.join(update_fields)}
                WHERE batch_id = %s AND product_url = %s
            """

            cursor = self.db_conn.cursor()
            cursor.execute(query, values)
            cursor.close()

            print(f"[INFO] Updated {rank_type} for existing product")
            return True

        except Exception as e:
            print(f"[ERROR] Failed to update product rank: {e}")
            traceback.print_exc()
            return False

    # Cleanup target usage:
    # - amazon/hhp/amazon_hhp_dt_update.py: saves cookies during review crawling
    # - Mentioned in CLAUDE.md
    def save_cookies(self, account_name):
        """
        현재 세션의 쿠키를 파일로 저장

        쓰임새:
        - 첫 페이지 로드 후 쿠키를 저장하여 세션 유지
        - 다음 크롤링 시 같은 쿠키를 사용하여 일관된 결과 확보

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)

        Returns:
            None
        """
        try:
            # account_name 기반으로 쿠키 파일 경로 생성
            cookie_file = f'cookies/{account_name.lower()}_cookies.pkl'

            # cookies 디렉토리 생성
            os.makedirs('cookies', exist_ok=True)

            # 쿠키 저장
            cookies = self.driver.get_cookies()
            with open(cookie_file, 'wb') as f:
                pickle.dump(cookies, f)

            print(f"[INFO] Cookies saved to {cookie_file}")

        except Exception as e:
            print(f"[WARNING] Failed to save cookies: {e}")

    # Cleanup target usage:
    # - amazon/hhp/amazon_hhp_dt_update.py: initialize(), restart_driver()
    # - Mentioned in CLAUDE.md
    def load_cookies(self, account_name, suffix=None):
        """
        저장된 쿠키를 로드하여 세션 복원

        쓰임새:
        - 이전에 저장한 쿠키를 로드하여 같은 세션으로 크롤링
        - 일관된 제품 목록 순서 유지
        - Amazon Detail 크롤러: 로그인 세션 복원 (리뷰 수집용)

        Args:
            account_name (str): 쇼핑몰명 (Amazon, Bestbuy, Walmart)
            suffix (str): 쿠키 파일 번호 (Amazon 전용, 예: '1', '2', '3')

        Returns:
            bool: 쿠키 로드 성공 시 True, 실패 시 False
        """
        try:
            # account_name 기반으로 쿠키 파일 경로 생성 (Amazon은 suffix 지원)
            if account_name == 'Amazon' and suffix:
                cookie_file = f'cookies/{account_name.lower()}_cookies_{suffix}.pkl'
            else:
                cookie_file = f'cookies/{account_name.lower()}_cookies.pkl'

            if not os.path.exists(cookie_file):
                print(f"[INFO] No saved cookies found at {cookie_file}")
                if account_name == 'Amazon':
                    print(f"[WARNING] Amazon login cookies not found")
                    print(f"[WARNING] Review collection may fail without login")
                    print(f"[INFO] To create cookies, run: python amazon_login.py")
                return False

            # 쿠키 로드
            with open(cookie_file, 'rb') as f:
                cookies = pickle.load(f)

            # 도메인에 먼저 접속 (쿠키를 추가하기 전에 필요)
            if account_name == 'Amazon':
                self.driver.get('https://www.amazon.com')
            elif account_name == 'Bestbuy':
                self.driver.get('https://www.bestbuy.com')
            elif account_name == 'Walmart':
                self.driver.get('https://www.walmart.com')
            time.sleep(2)

            # 기존 쿠키 삭제 후 새 쿠키 추가 (세션 충돌 방지)
            self.driver.delete_all_cookies()

            # 쿠키 추가
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass  # 일부 쿠키는 추가 실패할 수 있음

            print(f"[INFO] Cookies loaded from {cookie_file}")

            # Amazon인 경우 로그인 확인
            if account_name == 'Amazon':
                self.driver.refresh()
                time.sleep(2)
                try:
                    from selenium.webdriver.common.by import By
                    account_element = self.driver.find_element(By.ID, "nav-link-accountList")
                    account_text = account_element.text.lower()

                    if "hello" in account_text and "sign in" not in account_text:
                        print(f"[SUCCESS] Amazon login verified with cookies")
                    else:
                        print(f"[WARNING] Amazon cookies may be expired")
                        print(f"[INFO] If review collection fails, run: python amazon_login.py")
                except:
                    print(f"[INFO] Could not verify Amazon login status")

            return True

        except Exception as e:
            print(f"[WARNING] Failed to load cookies: {e}")
            return False
