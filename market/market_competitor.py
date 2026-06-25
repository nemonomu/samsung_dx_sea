"""
Market Competitor Analyzer - OpenAI 기반 경쟁제품 분석

OpenAI API를 활용한 경쟁사 제품 분석 크롤러
키워드 기반 경쟁제품 매칭 및 분석 결과 DB 저장

================================================================================
스케줄 기반 실행 (운영 모드)
================================================================================
- 경쟁 신제품 추출: 분기 첫날 (1/1, 4/1, 7/1, 10/1)에만 실행
- 이벤트 일정 추출: 매월 첫번째 월요일에만 실행
- 분기 첫날이 첫번째 월요일인 경우: 둘 다 실행
- 조건 미충족 시: 로그 없이 종료

================================================================================
테스트/DRY RUN 모드
================================================================================
- 't' 입력: 테스트 모드 (test_market_comp_product/event 테이블)
- 'd' 입력: DRY RUN 모드 (OpenAI 응답만 로그에 출력, DB 저장 안함)

================================================================================
필요 패키지
================================================================================
pip install openai psycopg2-binary

================================================================================
"""

import os
import sys
import time
import json
import traceback
import logging
import glob
import psycopg2
import msvcrt
from datetime import datetime
from openai import OpenAI

# 상위 디렉토리의 config.py 참조
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB_CONFIG, OPENAI_API_KEY

# ============================================================================
# 로그 설정
# ============================================================================

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, 'logs')
LOG_FILE = None
logger = None


def setup_logger():
    """로거 설정 (파일 + 콘솔 출력)"""
    global LOG_FILE, logger

    os.makedirs(LOG_DIR, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    LOG_FILE = os.path.join(LOG_DIR, f'market_competitor_{timestamp}.log')

    logger = logging.getLogger('market_competitor')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(console_handler)

    return LOG_FILE


def cleanup_old_logs(days=30):
    """오래된 로그 파일 정리"""
    try:
        log_pattern = os.path.join(LOG_DIR, 'market_competitor_*.log')
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


def get_timestamp():
    """현재 시간 반환"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def print_log(level, message):
    """로그 출력"""
    if logger:
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(message)
    else:
        timestamp = get_timestamp()
        print(f"[{timestamp}] [{level}] {message}")


def get_input_with_timeout(prompt, timeout=10):
    """타임아웃이 있는 입력 받기 (Windows용)"""
    sys.stdout.write(prompt)
    sys.stdout.flush()

    start_time = time.time()
    input_chars = []

    while True:
        elapsed = time.time() - start_time
        remaining = timeout - elapsed

        if remaining <= 0:
            print()
            return None

        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == '\r':
                print()
                return ''.join(input_chars)
            elif char == '\b':
                if input_chars:
                    input_chars.pop()
                    print('\b \b', end='', flush=True)
            else:
                input_chars.append(char)
                print(char, end='', flush=True)

        time.sleep(0.1)


# ============================================================================
# 스케줄 체크 함수
# ============================================================================

def is_quarter_first_day():
    """분기 첫날인지 확인 (1/1, 4/1, 7/1, 10/1)"""
    today = datetime.now()
    return today.day == 1 and today.month in [1, 4, 7, 10]


def is_first_monday_of_month():
    """이번 달 첫번째 월요일인지 확인"""
    today = datetime.now()
    # 월요일(weekday=0)이고 7일 이하면 첫번째 월요일
    return today.weekday() == 0 and today.day <= 7


# ============================================================================
# 데이터베이스 클래스
# ============================================================================

class DatabaseManager:
    """데이터베이스 연결 및 쿼리 관리"""

    def __init__(self, test_mode=False):
        self.conn = None
        self.cursor = None
        self.test_mode = test_mode
        self.table_name = 'test_market_comp_product' if test_mode else 'market_comp_product'

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

    def get_samsung_products(self, product_line=None, limit=None):
        """
        Samsung 제품 키워드 조회 (content_type = 'samsung')

        Returns:
            list: [(id, product_line, keyword), ...]
        """
        query = """
            SELECT id, product_line, keyword
            FROM market_mst
            WHERE is_active = true
              AND analysis_type = 'competitor'
              AND content_type = 'samsung'
        """
        params = []

        if product_line:
            query += " AND product_line = %s"
            params.append(product_line)

        query += " ORDER BY id"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        self.execute(query, params if params else None)
        return self.fetchall()

    def get_competitor_brands(self, product_line=None, limit=None):
        """
        경쟁사 브랜드 조회 (content_type = 'comp')

        Returns:
            list: [(id, product_line, keyword), ...]
        """
        query = """
            SELECT id, product_line, keyword
            FROM market_mst
            WHERE is_active = true
              AND analysis_type = 'competitor'
              AND content_type = 'comp'
        """
        params = []

        if product_line:
            query += " AND product_line = %s"
            params.append(product_line)

        query += " ORDER BY id"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        self.execute(query, params if params else None)
        return self.fetchall()

    def save_single_result(self, result, calendar_week, batch_id=None):
        """단일 결과 즉시 저장"""
        insert_query = f"""
            INSERT INTO {self.table_name} (
                country, samsung_series_name, comp_brand, comp_series_name,
                expected_release, release_status, comment, calender_week, created_at, batch_id, response_json, category
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            # response_json을 JSON 문자열로 변환
            response_json = result.get('response_json')
            if response_json and isinstance(response_json, str):
                # 이미 문자열이면 그대로 사용 (PostgreSQL JSONB가 자동 파싱)
                pass
            elif response_json:
                response_json = json.dumps(response_json)

            self.cursor.execute(insert_query, (
                'North America',
                result['samsung_product'],
                result['comp_brand'],
                result['comp_sku_name'],
                result.get('expected_release', ''),
                result.get('release_status', 'info_not_available'),
                result.get('comment', ''),
                calendar_week,
                result.get('created_at'),
                batch_id,
                response_json,
                result.get('product_line')  # 카테고리 (TV/HHP)
            ))
            self.commit()
            return True
        except Exception as e:
            print_log("ERROR", f"저장 실패: {result['samsung_product']} vs {result['comp_brand']}: {e}")
            self.rollback()
            return False


# ============================================================================
# OpenAI API 클래스
# ============================================================================

class OpenAIClient:
    """OpenAI API 클라이언트"""

    def __init__(self, api_key, db_manager):
        self.client = OpenAI(api_key=api_key)
        self.model = "gpt-5.4"
        self.db = db_manager
        self.template_id = None
        self.template = None

    def load_template(self, template_name):
        """DB에서 템플릿 조회"""
        try:
            query = """
                SELECT id, template
                FROM market_openai_templates
                WHERE template_name = %s AND is_active = true
                LIMIT 1
            """
            self.db.execute(query, (template_name,))
            row = self.db.fetchone()

            if row:
                self.template_id = row[0]
                self.template = row[1]
                print_log("INFO", f"템플릿 로드 완료: {template_name} (id: {self.template_id})")
                return True
            else:
                print_log("ERROR", f"템플릿을 찾을 수 없음: {template_name}")
                return False
        except Exception as e:
            print_log("ERROR", f"템플릿 로드 실패: {e}")
            return False

    def calculate_cost(self, prompt_tokens, completion_tokens):
        """GPT-4o 토큰 비용 계산 (USD)"""
        input_cost = (prompt_tokens / 1_000_000) * 2.50
        output_cost = (completion_tokens / 1_000_000) * 10.00
        return round(input_cost + output_cost, 6)

    def save_request(self, prompt, response_text, status, batch_id, error_message=None, tokens_used=None, cost_usd=None):
        """market_openai_request 테이블에 요청/응답 저장"""
        try:
            query = """
                INSERT INTO market_openai_request
                (template_id, question_sent, response_json, status, batch_id,
                 requested_at, completed_at, error_message, tokens_used, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            requested_at = datetime.now()
            completed_at = datetime.now() if status in ('success', 'error') else None

            response_json = None
            if response_text:
                try:
                    response_json = json.dumps(json.loads(response_text))
                except json.JSONDecodeError:
                    response_json = json.dumps({"raw_response": response_text})

            self.db.execute(query, (
                self.template_id,
                prompt,
                response_json,
                status,
                batch_id,
                requested_at,
                completed_at,
                error_message,
                tokens_used,
                cost_usd
            ))
            self.db.commit()
            print_log("INFO", f"  -> 요청/응답 저장 완료 (market_openai_request)")
        except Exception as e:
            print_log("ERROR", f"요청/응답 저장 실패: {e}")
            self.db.rollback()

    def generate_prompt(self, category, samsung_product, competitor_brands):
        """프롬프트 생성 (DB 템플릿 사용)"""
        if not self.template:
            print_log("ERROR", "템플릿이 로드되지 않았습니다.")
            return None

        today_date = datetime.now().strftime('%Y-%m-%d')

        prompt = self.template.format(
            today_date=today_date,
            category=category,
            samsung_product=samsung_product,
            competitor_brands=competitor_brands
        )
        return prompt

    def analyze(self, category, samsung_product, competitor_brands, batch_id=None, dry_run=False):
        """OpenAI Responses API로 경쟁제품 분석 (웹 검색 활성화)"""
        prompt = self.generate_prompt(category, samsung_product, competitor_brands)

        if not prompt:
            return {
                'success': False,
                'prompt': None,
                'response': None,
                'error': '템플릿 로드 실패'
            }

        # 웹 검색용 프롬프트에 JSON 출력 지시 추가
        web_prompt = f"""You are a market analyst specializing in consumer electronics.
Use web search to find the latest information about competitor products.
Always respond with valid JSON format only, no additional text.

{prompt}"""

        try:
            response = self.client.responses.create(
                model=self.model,
                tools=[{"type": "web_search_preview"}],
                input=web_prompt,
                temperature=0
            )

            response_text = response.output_text
            response_time = datetime.now()
            tokens_used = response.usage.total_tokens if response.usage else 0
            input_tokens = response.usage.input_tokens if response.usage else 0
            output_tokens = response.usage.output_tokens if response.usage else 0
            cost_usd = self.calculate_cost(input_tokens, output_tokens)

            # JSON 파싱 검증 및 추출
            try:
                # 응답에서 JSON 부분만 추출 (웹 검색 결과에 추가 텍스트가 있을 수 있음)
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                if json_start != -1 and json_end > json_start:
                    response_text = response_text[json_start:json_end]
                json.loads(response_text)
            except json.JSONDecodeError:
                print_log("WARNING", "응답이 유효한 JSON이 아닙니다. 원본 텍스트 저장")

            # 요청/응답 저장 (DRY RUN이 아닐 때만)
            if not dry_run:
                self.save_request(prompt, response_text, 'success', batch_id, None, tokens_used, cost_usd)

            return {
                'success': True,
                'prompt': prompt,
                'response': response_text,
                'tokens_used': tokens_used,
                'response_time': response_time
            }

        except Exception as e:
            print_log("ERROR", f"OpenAI Responses API 호출 실패: {e}")

            # 에러 시에도 저장 (DRY RUN이 아닐 때만)
            if not dry_run:
                self.save_request(prompt, None, 'error', batch_id, str(e), None, None)

            return {
                'success': False,
                'prompt': prompt,
                'response': None,
                'error': str(e)
            }


# ============================================================================
# 경쟁제품 분석기
# ============================================================================

class CompetitorAnalyzer:
    """경쟁제품 분석기"""

    def __init__(self, test_mode=False, test_product_line=None, test_count=None, dry_run=False, batch_id=None):
        self.test_mode = test_mode
        self.test_product_line = test_product_line
        self.test_count = test_count
        self.dry_run = dry_run
        self.db = DatabaseManager(test_mode=test_mode)
        self.openai = None
        self.batch_id = batch_id

    def setup(self):
        """초기화"""
        if not self.db.connect():
            return False

        # OpenAI 클라이언트 초기화
        try:
            self.openai = OpenAIClient(OPENAI_API_KEY, self.db)
            print_log("INFO", "OpenAI 클라이언트 초기화 완료")

            # 템플릿 로드
            if not self.openai.load_template('Market_comp_product'):
                return False

        except Exception as e:
            print_log("ERROR", f"OpenAI 클라이언트 초기화 실패: {e}")
            return False

        return True

    def cleanup(self):
        """정리"""
        self.db.disconnect()

    def generate_calendar_week(self):
        """캘린더 주차 생성 (예: w48)"""
        now = datetime.now()
        week_number = now.isocalendar()[1]
        return f"w{week_number}"

    def analyze_single_brand(self, product_line, samsung_product, competitor_brand):
        """단일 분석 (Samsung 제품 1개 vs 경쟁사 브랜드 1개)

        Args:
            product_line: 제품 라인 (TV/HHP)
            samsung_product: 삼성 제품 키워드
            competitor_brand: 경쟁사 브랜드 (단일 문자열, 예: 'LG')

        Returns:
            tuple: (results_list, response_json)
                   results_list: 경쟁 제품 리스트 (OpenAI가 여러 개 반환 시 모두 포함)
        """
        try:
            print_log("INFO", f"  분석 중: {samsung_product} vs {competitor_brand}")

            result = self.openai.analyze(product_line, samsung_product, competitor_brand, batch_id=self.batch_id, dry_run=self.dry_run)

            if result['success']:
                print_log("INFO", f"    -> 완료 (토큰: {result['tokens_used']})")

                # OpenAI 응답에서 competitor_analysis 배열 추출
                try:
                    response_data = json.loads(result['response'])
                    competitor_analysis = response_data.get('competitor_analysis', [])

                    # 모든 항목을 리스트로 반환
                    if competitor_analysis:
                        results = []
                        for comp in competitor_analysis:
                            results.append({
                                'samsung_product': samsung_product,
                                'comp_brand': comp.get('brand', competitor_brand),
                                'comp_sku_name': comp.get('comp_sku_name', 'info_not_available'),
                                'expected_release': comp.get('expected_release', ''),
                                'release_status': comp.get('release_status', 'info_not_available'),
                                'comment': comp.get('comment', ''),
                                'product_line': product_line,
                                'response_json': result['response'],
                                'success': True,
                                'created_at': result.get('response_time')
                            })
                        product_names = [r['comp_sku_name'] for r in results]
                        print_log("INFO", f"    -> {len(results)}개 경쟁제품 발견 ({', '.join(product_names)})")
                        return results, result['response']

                except (json.JSONDecodeError, TypeError) as e:
                    print_log("WARNING", f"JSON 파싱 실패: {e}")

                # 파싱 실패 시
                return [{
                    'samsung_product': samsung_product,
                    'comp_brand': competitor_brand,
                    'comp_sku_name': 'info_not_available',
                    'expected_release': '',
                    'release_status': 'info_not_available',
                    'comment': '',
                    'product_line': product_line,
                    'response_json': result['response'],
                    'success': True,
                    'created_at': result.get('response_time')
                }], result['response']

            else:
                print_log("WARNING", f"    -> 분석 실패: {result.get('error', 'Unknown error')}")
                return [{
                    'samsung_product': samsung_product,
                    'comp_brand': competitor_brand,
                    'comp_sku_name': 'info_not_available',
                    'expected_release': '',
                    'release_status': 'info_not_available',
                    'comment': '',
                    'product_line': product_line,
                    'response_json': None,
                    'success': False
                }], None

        except Exception as e:
            print_log("ERROR", f"분석 실패 ({samsung_product} vs {competitor_brand}): {e}")
            traceback.print_exc()
            return [{
                'samsung_product': samsung_product,
                'comp_brand': competitor_brand,
                'comp_sku_name': 'info_not_available',
                'expected_release': '',
                'release_status': 'info_not_available',
                'comment': '',
                'product_line': product_line,
                'response_json': None,
                'success': False
            }], None

    def analyze_all_products(self, samsung_products, competitor_brands, calendar_week, dry_run=False):
        """모든 Samsung 제품 분석 (카테고리별 → 제품별 → 브랜드별)

        Args:
            samsung_products: [(id, product_line, keyword), ...]
            competitor_brands: [(id, product_line, keyword), ...]
            calendar_week: 캘린더 주차
            dry_run: True이면 DB 저장 없이 OpenAI 응답만 로그에 출력

        Returns:
            tuple: (success_count, fail_count, comp_products_list)
                   comp_products_list는 dry_run일 때만 반환 (이벤트 분석용)
        """
        total_success = 0
        total_fail = 0
        dry_run_products = []  # DRY RUN용 제품 목록 (brand, product_name) 튜플

        # 카테고리별 Samsung 제품 그룹화
        samsung_by_category = {}
        for item in samsung_products:
            _, pl, keyword = item
            if pl not in samsung_by_category:
                samsung_by_category[pl] = []
            samsung_by_category[pl].append(keyword)

        # 카테고리별 경쟁사 브랜드 그룹화
        comp_by_category = {}
        for _, pl, keyword in competitor_brands:
            if pl not in comp_by_category:
                comp_by_category[pl] = []
            comp_by_category[pl].append(keyword)

        # 고정 카테고리 목록
        CATEGORIES = ['TV', 'HHP']

        # 카테고리별로 처리
        for category in CATEGORIES:
            samsung_list = samsung_by_category.get(category, [])
            comp_brands = comp_by_category.get(category, [])

            if not samsung_list:
                print_log("INFO", f"[{category}] Samsung 제품 없음, 스킵")
                continue

            if not comp_brands:
                print_log("WARNING", f"[{category}] 경쟁사 브랜드 없음, 스킵")
                continue

            print_log("INFO", f"\n{'='*60}")
            print_log("INFO", f"[{category}] 분석 시작 - Samsung {len(samsung_list)}개 × 경쟁사 {len(comp_brands)}개 = {len(samsung_list) * len(comp_brands)}회 API 호출")
            print_log("INFO", f"[{category}] 경쟁사: {', '.join(comp_brands)}")
            print_log("INFO", f"{'='*60}")

            # 해당 카테고리의 Samsung 제품별로 분석
            for idx, samsung_keyword in enumerate(samsung_list, 1):
                print_log("INFO", f"\n[Samsung {idx}/{len(samsung_list)}] {samsung_keyword}")

                # 각 경쟁사 브랜드별로 개별 API 호출
                for comp_brand in comp_brands:

                    # Samsung 1개 vs 경쟁사 1개 분석 (결과는 리스트로 반환)
                    results_list, response_json = self.analyze_single_brand(category, samsung_keyword, comp_brand)

                    # 결과 리스트 순회하며 처리
                    for result in results_list:
                        if dry_run:
                            print_log("INFO", f"[DRY RUN] {samsung_keyword} vs {comp_brand}: {result['comp_sku_name']}")
                            if response_json:
                                print_log("INFO", f"[DRY RUN] 응답: {response_json}")
                            # DRY RUN용 제품 목록 수집 (info_not_available 제외)
                            if result['success'] and result['comp_sku_name'] != 'info_not_available':
                                dry_run_products.append((result['comp_brand'], result['comp_sku_name']))
                            if result['success']:
                                total_success += 1
                            else:
                                total_fail += 1
                        else:
                            # 성공한 결과 즉시 저장
                            if result['success']:
                                if self.db.save_single_result(result, calendar_week, self.batch_id):
                                    total_success += 1
                                else:
                                    total_fail += 1
                            else:
                                total_fail += 1

                    # API 요청 간격 (Rate limit 방지)
                    time.sleep(1)

        # DRY RUN일 때는 제품 목록도 반환 (중복 제거 - 튜플이므로 set 사용 가능)
        if dry_run:
            unique_products = list(set(dry_run_products))
            return total_success, total_fail, unique_products

        return total_success, total_fail, None

    def print_summary(self, success_count, fail_count, total_count):
        """결과 요약 출력"""
        mode_str = "테스트 모드" if self.test_mode else "운영 모드"
        print("\n" + "=" * 60)
        print("경쟁제품 분석 완료")
        print("=" * 60)
        print(f"모드: {mode_str}")
        print(f"배치 ID: {self.batch_id}")
        print(f"테이블: {self.db.table_name}")
        print(f"성공: {success_count}건")
        print(f"실패: {fail_count}건")
        print(f"총계: {total_count}건")

    def run(self):
        """메인 실행"""
        mode_str = "테스트 모드" if self.test_mode else "운영 모드"
        dry_run_str = " [DRY RUN - DB 저장 안함]" if self.dry_run else ""

        print("\n" + "=" * 60)
        print(f"Market Competitor Analyzer ({mode_str}){dry_run_str}")
        print(f"배치 ID: {self.batch_id}")
        print(f"저장 테이블: {self.db.table_name}")
        print("*** Responses API + web_search_preview 사용 ***")
        if self.dry_run:
            print("*** DRY RUN 모드: OpenAI 응답만 로그에 출력, DB 저장 안함 ***")
        if LOG_FILE:
            print(f"로그 파일: {LOG_FILE}")
        print("=" * 60)

        try:
            if not self.setup():
                return

            # Samsung 제품 조회 (content_type = 'samsung')
            samsung_products = self.db.get_samsung_products(
                product_line=self.test_product_line,
                limit=self.test_count if self.test_mode else None
            )

            # 경쟁사 브랜드 조회 (content_type = 'comp')
            competitor_brands = self.db.get_competitor_brands(
                product_line=self.test_product_line
            )

            # 필터 정보 출력
            if self.test_mode:
                filter_info = []
                if self.test_product_line:
                    filter_info.append(f"product_line={self.test_product_line}")
                if self.test_count:
                    filter_info.append(f"samsung_limit={self.test_count}")
                if filter_info:
                    print_log("INFO", f"테스트 필터: {', '.join(filter_info)}")

            if not samsung_products:
                print_log("INFO", "Samsung 제품이 없습니다. (content_type='samsung' 확인)")
                return

            if not competitor_brands:
                print_log("INFO", "경쟁사 브랜드가 없습니다. (content_type='comp' 확인)")
                return

            # 조합 수 계산 (같은 product_line만)
            total_combinations = sum(
                1 for _, pl, _ in samsung_products
                for _, cpl, _ in competitor_brands
                if pl == cpl
            )

            print_log("INFO", f"Samsung 제품: {len(samsung_products)}개")
            print_log("INFO", f"경쟁사 브랜드: {len(competitor_brands)}개")
            print_log("INFO", f"분석 조합 수: {total_combinations}개")

            calendar_week = self.generate_calendar_week()
            success, fail, dry_run_products = self.analyze_all_products(
                samsung_products, competitor_brands, calendar_week, dry_run=self.dry_run
            )
            self.print_summary(success, fail, total_combinations)

            # DRY RUN일 때 제품 목록 반환
            return dry_run_products

        except Exception as e:
            print_log("ERROR", f"실행 오류: {e}")
            traceback.print_exc()
            return None

        finally:
            self.cleanup()


# ============================================================================
# 이벤트 날짜 분석기
# ============================================================================

class EventDateAnalyzer:
    """경쟁제품 이벤트 날짜 분석기"""

    def __init__(self, test_mode=False, limit=None, dry_run=False, source_batch_id=None, batch_id=None):
        self.test_mode = test_mode
        self.limit = limit
        self.dry_run = dry_run
        self.source_batch_id = source_batch_id  # CompetitorAnalyzer의 batch_id (DB 조회용)
        self.db = DatabaseManager(test_mode=test_mode)
        self.openai = None
        self.event_table_name = 'test_market_comp_event' if test_mode else 'market_comp_event'
        self.template_id = None
        self.template = None
        self.batch_id = batch_id

    def setup(self):
        """초기화"""
        if not self.db.connect():
            return False

        try:
            self.openai = OpenAIClient(OPENAI_API_KEY, self.db)
            print_log("INFO", "OpenAI 클라이언트 초기화 완료")

            # 템플릿 로드
            if not self.openai.load_template('Market_comp_event'):
                return False

        except Exception as e:
            print_log("ERROR", f"OpenAI 클라이언트 초기화 실패: {e}")
            return False

        return True

    def cleanup(self):
        """정리"""
        self.db.disconnect()

    def get_competitor_products(self):
        """market_comp_product 테이블에서 경쟁제품 조회 (brand, product_name, category 튜플)

        - source_batch_id가 지정되면 해당 배치만 조회
        - source_batch_id가 None이면 MAX(batch_id) 사용 (최신 배치)
        """
        # source_batch_id가 지정되면 해당 배치, 아니면 최신 배치 사용
        if self.source_batch_id:
            batch_condition = "batch_id = %s"
            params = [self.source_batch_id]
        else:
            batch_condition = f"batch_id = (SELECT MAX(batch_id) FROM {self.db.table_name})"
            params = []

        query = f"""
            SELECT DISTINCT comp_brand, comp_series_name, category
            FROM {self.db.table_name}
            WHERE {batch_condition}
              AND comp_series_name IS NOT NULL
              AND comp_series_name != ''
              AND comp_series_name != 'info_not_available'
        """

        if self.limit:
            query += f" LIMIT {self.limit}"

        self.db.execute(query, params if params else None)
        return [(row[0], row[1], row[2]) for row in self.db.fetchall()]

    def generate_event_prompt(self, product_name):
        """이벤트 날짜 분석 프롬프트 생성 (DB 템플릿 사용)"""
        if not self.openai.template:
            print_log("ERROR", "템플릿이 로드되지 않았습니다.")
            return None

        prompt = self.openai.template.format(product_name=product_name)
        return prompt

    def analyze_event(self, product_name):
        """OpenAI Responses API로 이벤트 날짜 분석 (웹 검색 활성화)"""
        prompt = self.generate_event_prompt(product_name)

        if not prompt:
            return {
                'success': False,
                'response': None,
                'error': '템플릿 로드 실패'
            }

        # 웹 검색용 프롬프트에 JSON 출력 지시 추가
        web_prompt = f"""You are a product launch analyst specializing in consumer electronics.
Use web search to find the latest information about product launch dates and pre-order schedules for the North American market.
Always respond with valid JSON format only, no additional text.

{prompt}"""

        try:
            response = self.openai.client.responses.create(
                model=self.openai.model,
                tools=[{"type": "web_search_preview"}],
                input=web_prompt,
                temperature=0
            )

            response_text = response.output_text
            response_time = datetime.now()
            tokens_used = response.usage.total_tokens if response.usage else 0
            input_tokens = response.usage.input_tokens if response.usage else 0
            output_tokens = response.usage.output_tokens if response.usage else 0
            cost_usd = self.openai.calculate_cost(input_tokens, output_tokens)

            # JSON 파싱 검증 및 추출
            try:
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                if json_start != -1 and json_end > json_start:
                    response_text = response_text[json_start:json_end]
                json.loads(response_text)
            except json.JSONDecodeError:
                print_log("WARNING", "응답이 유효한 JSON이 아닙니다. 원본 텍스트 저장")

            # 요청/응답 저장 (DRY RUN이 아닐 때만)
            if not self.dry_run:
                self.openai.save_request(prompt, response_text, 'success', self.batch_id, None, tokens_used, cost_usd)

            return {
                'success': True,
                'prompt': prompt,
                'response': response_text,
                'tokens_used': tokens_used,
                'response_time': response_time
            }

        except Exception as e:
            print_log("ERROR", f"OpenAI Responses API 호출 실패: {e}")

            # 에러 시에도 저장 (DRY RUN이 아닐 때만)
            if not self.dry_run:
                self.openai.save_request(prompt, None, 'error', self.batch_id, str(e), None, None)

            return {
                'success': False,
                'prompt': prompt,
                'response': None,
                'error': str(e)
            }

    def save_event_result(self, result_data, calendar_week, comp_brand, response_json=None, category=None):
        """이벤트 분석 결과 저장"""
        # rumor_based 필드 추출
        rumor_based = result_data.get('rumor_based', {})
        rumor_release_window = rumor_based.get('rumor_release_window') if rumor_based else None
        rumor_preorder_window = rumor_based.get('rumor_preorder_window') if rumor_based else None
        rumor_main_sources = rumor_based.get('main_sources') if rumor_based else None
        rumor_confidence_level = rumor_based.get('confidence_level') if rumor_based else None

        insert_query = f"""
            INSERT INTO {self.event_table_name} (
                country, comp_brand, comp_sku_name, comp_launch_date, comp_preorder,
                comp_pre_order_start_date, comp_preorder_end_date,
                rumor_release_window, rumor_preorder_window, rumor_main_sources, rumor_confidence_level,
                calender_week, created_at, batch_id, response_json, category
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            self.db.cursor.execute(insert_query, (
                'North America',
                comp_brand,
                result_data.get('comp_sku_name'),
                result_data.get('comp_launch_date'),
                result_data.get('comp_preorder'),
                result_data.get('comp_pre_order_start_date'),
                result_data.get('comp_preorder_end_date'),
                rumor_release_window,
                rumor_preorder_window,
                rumor_main_sources,
                rumor_confidence_level,
                calendar_week,
                result_data.get('created_at'),
                self.batch_id,
                response_json,
                category  # 카테고리 (TV/HHP)
            ))
            self.db.commit()
            return True
        except Exception as e:
            print_log("ERROR", f"이벤트 결과 저장 실패: {e}")
            self.db.rollback()
            return False

    def generate_calendar_week(self):
        """캘린더 주차 생성"""
        now = datetime.now()
        week_number = now.isocalendar()[1]
        return f"w{week_number}"

    def run(self, products_from_memory=None):
        """메인 실행

        Args:
            products_from_memory: DRY RUN 모드에서 메모리로 전달받은 제품 목록
        """
        mode_str = "테스트 모드" if self.test_mode else "운영 모드"
        dry_run_str = " [DRY RUN]" if self.dry_run else ""

        print("\n" + "=" * 60)
        print(f"Event Date Analyzer ({mode_str}){dry_run_str}")
        print(f"배치 ID: {self.batch_id}")
        if products_from_memory:
            print(f"소스: 메모리 (CompetitorAnalyzer 결과)")
        else:
            print(f"소스 테이블: {self.db.table_name}")
            if self.source_batch_id:
                print(f"소스 배치 ID: {self.source_batch_id}")
        print(f"저장 테이블: {self.event_table_name}")
        print("=" * 60)

        try:
            if not self.setup():
                return 0, 0

            # 경쟁제품 목록 조회 (메모리 우선, 없으면 DB)
            if products_from_memory:
                products = products_from_memory
                print_log("INFO", "메모리에서 제품 목록 사용")
            else:
                products = self.get_competitor_products()

            if not products:
                print_log("INFO", "분석할 경쟁제품이 없습니다.")
                return 0, 0

            print_log("INFO", f"분석 대상 제품: {len(products)}개")

            calendar_week = self.generate_calendar_week()
            total_success = 0
            total_fail = 0

            for idx, product_tuple in enumerate(products, 1):
                # 튜플 길이에 따라 category 처리 (호환성)
                if len(product_tuple) >= 3:
                    comp_brand, product_name, category = product_tuple[0], product_tuple[1], product_tuple[2]
                else:
                    comp_brand, product_name = product_tuple[0], product_tuple[1]
                    category = None

                print(f"\n[{idx}/{len(products)}] ", end="")
                print_log("INFO", f"이벤트 분석 중: {comp_brand} - {product_name} ({category or 'N/A'})")

                result = self.analyze_event(product_name)

                if result['success']:
                    print_log("INFO", f"  -> 분석 완료 (토큰: {result['tokens_used']})")

                    try:
                        response_data = json.loads(result['response'])
                        response_data['created_at'] = result.get('response_time')

                        if self.dry_run:
                            print_log("INFO", f"[DRY RUN] 응답: {result['response']}")
                            total_success += 1
                        else:
                            if self.save_event_result(response_data, calendar_week, comp_brand, result['response'], category):
                                total_success += 1
                            else:
                                total_fail += 1

                    except json.JSONDecodeError as e:
                        print_log("WARNING", f"JSON 파싱 실패: {e}")
                        total_fail += 1
                else:
                    print_log("WARNING", f"  -> 분석 실패: {result.get('error', 'Unknown error')}")
                    total_fail += 1

                # API 요청 간격
                time.sleep(1)

            # 결과 출력
            print("\n" + "=" * 60)
            print("이벤트 분석 완료")
            print("=" * 60)
            print(f"모드: {mode_str}")
            print(f"배치 ID: {self.batch_id}")
            print(f"테이블: {self.event_table_name}")
            print(f"성공: {total_success}건")
            print(f"실패: {total_fail}건")

            return total_success, total_fail

        except Exception as e:
            print_log("ERROR", f"실행 오류: {e}")
            traceback.print_exc()
            return 0, 0

        finally:
            self.cleanup()


# ============================================================================
# 메인
# ============================================================================

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Market Competitor & Event Analyzer (OpenAI)")
    print("=" * 60)
    print("\n[모드 선택]")
    print("  - 't' 입력: 테스트 모드 (test_market_comp_product/event 테이블)")
    print("  - 'd' 입력: DRY RUN 모드 (OpenAI 응답만 로그에 출력, DB 저장 안함)")
    print("  - 10초 내 입력 없음: 운영 모드 (스케줄 기반 자동 실행)")
    print()

    user_input = get_input_with_timeout("모드 선택 (t=테스트, d=DRY RUN, 10초 후 자동 운영모드): ", timeout=10)

    if user_input and user_input.lower().strip() == 'd':
        # DRY RUN 모드
        print_log("INFO", "DRY RUN 모드로 실행합니다. (DB 저장 안함)")

        print("\n[DRY RUN 필터 설정] (엔터: 전체)")
        test_product_line = input("  product_line (TV/HHP): ").strip() or None
        test_count_input = input("  test_count: ").strip()
        test_count = int(test_count_input) if test_count_input else None

        # 1단계: 경쟁제품 분석
        competitor_analyzer = CompetitorAnalyzer(
            test_mode=True,
            test_product_line=test_product_line,
            test_count=test_count,
            dry_run=True
        )
        dry_run_products = competitor_analyzer.run()

        # 2단계: 이벤트 날짜 분석 (메모리에서 제품 목록 전달)
        event_analyzer = EventDateAnalyzer(
            test_mode=True,
            limit=test_count,
            dry_run=True
        )
        event_analyzer.run(products_from_memory=dry_run_products)

        input("\n엔터키를 누르면 종료합니다...")

    elif user_input and user_input.lower().strip() == 't':
        # 테스트 모드: 로거 설정 (오래된 로그 삭제 안함)
        setup_logger()

        test_mode = True
        print_log("INFO", "테스트 모드로 실행합니다.")

        print("\n[테스트 필터 설정] (엔터: 전체)")
        test_product_line = input("  product_line (TV/HHP): ").strip() or None
        test_count_input = input("  test_count: ").strip()
        test_count = int(test_count_input) if test_count_input else None

        # 공통 배치 ID 생성
        batch_id = f"t_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print_log("INFO", f"배치 ID: {batch_id}")

        # 1단계: 경쟁제품 분석
        competitor_analyzer = CompetitorAnalyzer(
            test_mode=test_mode,
            test_product_line=test_product_line,
            test_count=test_count,
            batch_id=batch_id
        )
        competitor_analyzer.run()

        # 2단계: 이벤트 날짜 분석 (동일 batch_id 사용)
        event_analyzer = EventDateAnalyzer(
            test_mode=test_mode,
            source_batch_id=batch_id,
            batch_id=batch_id
        )
        event_analyzer.run()

        input("\n엔터키를 누르면 종료합니다...")

    else:
        # ================================================================
        # 운영 모드: 스케줄 기반 실행
        # ================================================================
        # - 경쟁 신제품 추출: 분기 첫날 (1/1, 4/1, 7/1, 10/1)
        # - 이벤트 일정 추출: 매월 첫번째 월요일
        # - 조건 미충족 시: 로그 없이 조용히 종료
        # ================================================================

        run_competitor = is_quarter_first_day()
        run_event = is_first_monday_of_month()

        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d (%A)')

        # 실행 조건 미충족 시 로그 없이 종료
        if not run_competitor and not run_event:
            print(f"[{today_str}] 실행 조건 미충족 - 스킵")
            print("  - 경쟁 신제품 추출: 분기 첫날 (1/1, 4/1, 7/1, 10/1)")
            print("  - 이벤트 일정 추출: 매월 첫번째 월요일")
            sys.exit(0)

        # 실행 조건 충족 시 로거 설정
        setup_logger()
        cleanup_old_logs()

        print_log("INFO", f"운영 모드 - 스케줄 기반 실행 ({today_str})")
        print_log("INFO", f"  - 경쟁 신제품 추출: {'실행' if run_competitor else '스킵'}")
        print_log("INFO", f"  - 이벤트 일정 추출: {'실행' if run_event else '스킵'}")

        # 공통 배치 ID 생성
        batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        print_log("INFO", f"배치 ID: {batch_id}")

        # 1단계: 경쟁 신제품 추출 (분기 첫날에만 실행)
        if run_competitor:
            print_log("INFO", "=" * 60)
            print_log("INFO", "[1단계] 경쟁 신제품 추출 시작")
            print_log("INFO", "=" * 60)

            competitor_analyzer = CompetitorAnalyzer(
                test_mode=False,
                batch_id=batch_id
            )
            competitor_analyzer.run()

        # 2단계: 이벤트 일정 추출 (첫번째 월요일에만 실행)
        if run_event:
            print_log("INFO", "=" * 60)
            print_log("INFO", "[2단계] 이벤트 일정 추출 시작")
            print_log("INFO", "=" * 60)

            # source_batch_id: 경쟁제품도 실행했으면 같은 batch_id, 아니면 None (MAX batch_id 사용)
            source_batch_id = batch_id if run_competitor else None

            event_analyzer = EventDateAnalyzer(
                test_mode=False,
                source_batch_id=source_batch_id,
                batch_id=batch_id
            )
            event_analyzer.run()

        print_log("INFO", "=" * 60)
        print_log("INFO", "스케줄 실행 완료")
        print_log("INFO", "=" * 60)
        # 운영모드는 자동 종료
