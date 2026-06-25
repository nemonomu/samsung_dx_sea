"""
Market Competitor Analyzer - Backfill (Product Only)

OpenAI API를 활용한 경쟁사 제품 분석 크롤러 (백필용)
키워드를 선택하여 특정 삼성 제품 및 경쟁사 브랜드에 대해서만 수집을 수행합니다.
"""

import os
import sys
import time
import json
import traceback
import logging
import psycopg2
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
    LOG_FILE = os.path.join(LOG_DIR, f'market_competitor_backfill_{timestamp}.log')

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

    def get_samsung_products(self, product_line=None, keywords=None, limit=None):
        """
        Samsung 제품 키워드 조회 (content_type = 'samsung')
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

        if keywords:
            placeholders = ', '.join(['%s'] * len(keywords))
            query += f" AND keyword IN ({placeholders})"
            params.extend(keywords)

        query += " ORDER BY id"

        if limit:
            query += " LIMIT %s"
            params.append(limit)

        self.execute(query, params if params else None)
        return self.fetchall()

    def get_competitor_brands(self, product_line=None, keywords=None, limit=None):
        """
        경쟁사 브랜드 조회 (content_type = 'comp')
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

        if keywords:
            placeholders = ', '.join(['%s'] * len(keywords))
            query += f" AND keyword IN ({placeholders})"
            params.extend(keywords)

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
            response_json = result.get('response_json')
            if response_json and isinstance(response_json, str):
                pass
            elif response_json:
                response_json = json.dumps(response_json)

            self.cursor.execute(insert_query, (
                'North America',
                result['samsung_product'],
                result['comp_brand'],
                result.get('comp_sku_name', 'info_not_available'),
                result.get('expected_release', ''),
                result.get('release_status', 'info_not_available'),
                result.get('comment', ''),
                calendar_week,
                result.get('created_at'),
                batch_id,
                response_json,
                result.get('product_line')
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

            try:
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                if json_start != -1 and json_end > json_start:
                    response_text = response_text[json_start:json_end]
                json.loads(response_text)
            except json.JSONDecodeError:
                print_log("WARNING", "응답이 유효한 JSON이 아닙니다. 원본 텍스트 저장")

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
    """경쟁제품 분석기 (백필용)"""

    def __init__(self, test_mode=False, test_product_line=None, target_samsung_keywords=None, target_comp_keywords=None, test_count=None, dry_run=False, batch_id=None):
        self.test_mode = test_mode
        self.test_product_line = test_product_line
        self.target_samsung_keywords = target_samsung_keywords
        self.target_comp_keywords = target_comp_keywords
        self.test_count = test_count
        self.dry_run = dry_run
        self.db = DatabaseManager(test_mode=test_mode)
        self.openai = None
        self.batch_id = batch_id

    def setup(self):
        """초기화"""
        if not self.db.connect():
            return False

        try:
            self.openai = OpenAIClient(OPENAI_API_KEY, self.db)
            print_log("INFO", "OpenAI 클라이언트 초기화 완료")

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
        """캘린더 주차 생성"""
        now = datetime.now()
        week_number = now.isocalendar()[1]
        return f"w{week_number}"

    def analyze_single_brand(self, product_line, samsung_product, competitor_brand):
        """단일 분석 (Samsung 제품 1개 vs 경쟁사 브랜드 1개)"""
        try:
            print_log("INFO", f"  분석 중: {samsung_product} vs {competitor_brand}")

            result = self.openai.analyze(product_line, samsung_product, competitor_brand, batch_id=self.batch_id, dry_run=self.dry_run)

            if result['success']:
                print_log("INFO", f"    -> 완료 (토큰: {result['tokens_used']})")

                try:
                    response_data = json.loads(result['response'])
                    competitor_analysis = response_data.get('competitor_analysis', [])

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
        """모든 Samsung 제품 분석"""
        total_success = 0
        total_fail = 0

        samsung_by_category = {}
        for item in samsung_products:
            _, pl, keyword = item
            if pl not in samsung_by_category:
                samsung_by_category[pl] = []
            samsung_by_category[pl].append(keyword)

        comp_by_category = {}
        for _, pl, keyword in competitor_brands:
            if pl not in comp_by_category:
                comp_by_category[pl] = []
            comp_by_category[pl].append(keyword)

        CATEGORIES = ['TV', 'HHP']

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

            for idx, samsung_keyword in enumerate(samsung_list, 1):
                print_log("INFO", f"\n[Samsung {idx}/{len(samsung_list)}] {samsung_keyword}")

                for comp_brand in comp_brands:

                    results_list, response_json = self.analyze_single_brand(category, samsung_keyword, comp_brand)

                    for result in results_list:
                        if dry_run:
                            print_log("INFO", f"[DRY RUN] {samsung_keyword} vs {comp_brand}: {result['comp_sku_name']}")
                            if response_json:
                                print_log("INFO", f"[DRY RUN] 응답: {response_json}")
                            if result['success']:
                                total_success += 1
                            else:
                                total_fail += 1
                        else:
                            if result['success']:
                                if self.db.save_single_result(result, calendar_week, self.batch_id):
                                    total_success += 1
                                else:
                                    total_fail += 1
                            else:
                                total_fail += 1

                    time.sleep(1)

        return total_success, total_fail, None

    def print_summary(self, success_count, fail_count, total_count):
        """결과 요약 출력"""
        mode_str = "테스트 모드" if self.test_mode else "운영 모드"
        print("\n" + "=" * 60)
        print("경쟁제품 백필 완료")
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
        print(f"Market Competitor Backfill ({mode_str}){dry_run_str}")
        print(f"배치 ID: {self.batch_id}")
        print(f"저장 테이블: {self.db.table_name}")
        if self.target_samsung_keywords: print(f"선택한 삼성 키워드: {', '.join(self.target_samsung_keywords)}")
        if self.target_comp_keywords: print(f"선택한 경쟁사 브랜드: {', '.join(self.target_comp_keywords)}")
        print("=" * 60)

        try:
            if not self.setup():
                return

            samsung_products = self.db.get_samsung_products(
                product_line=self.test_product_line,
                keywords=self.target_samsung_keywords,
                limit=self.test_count if self.test_mode else None
            )

            competitor_brands = self.db.get_competitor_brands(
                product_line=self.test_product_line,
                keywords=self.target_comp_keywords
            )

            if not samsung_products:
                print_log("INFO", "일치하는 Samsung 제품이 없습니다.")
                return

            if not competitor_brands:
                print_log("INFO", "일치하는 경쟁사 브랜드가 없습니다.")
                return

            total_combinations = sum(
                1 for _, pl, _ in samsung_products
                for _, cpl, _ in competitor_brands
                if pl == cpl
            )

            print_log("INFO", f"Samsung 제품: {len(samsung_products)}개")
            print_log("INFO", f"경쟁사 브랜드: {len(competitor_brands)}개")
            print_log("INFO", f"분석 조합 수: {total_combinations}개")

            calendar_week = self.generate_calendar_week()
            success, fail, _ = self.analyze_all_products(
                samsung_products, competitor_brands, calendar_week, dry_run=self.dry_run
            )
            self.print_summary(success, fail, total_combinations)

        except Exception as e:
            print_log("ERROR", f"실행 오류: {e}")
            traceback.print_exc()

        finally:
            self.cleanup()


# ============================================================================
# 메인
# ============================================================================

def select_from_list(items, label):
    """번호로 항목 선택 (복수 선택 가능)"""
    print(f"\n[{label} 선택]")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    print(f"  0. 전체 선택")
    selection = input("번호 입력 (쉼표로 복수 선택 가능, 0=전체): ").strip()

    if not selection or selection == '0':
        return None  # 전체 선택

    selected = []
    for num in selection.split(','):
        num = num.strip()
        if num.isdigit() and 1 <= int(num) <= len(items):
            selected.append(items[int(num) - 1])
    return selected if selected else None


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Market Competitor Backfill (Product Only)")
    print("=" * 60)

    # 1. 대상 카테고리
    product_line = input("대상 카테고리 (TV 또는 HHP, 공백입력시 전체): ").strip()
    if not product_line: product_line = None

    # DB 연결하여 키워드 목록 조회
    temp_db = DatabaseManager()
    if not temp_db.connect():
        print("DB 연결 실패. 종료합니다.")
        sys.exit(1)

    # 2. 삼성 제품 키워드 선택
    samsung_rows = temp_db.get_samsung_products(product_line=product_line)
    samsung_keyword_list = [row[2] for row in samsung_rows]  # keyword
    if not samsung_keyword_list:
        print("삼성 제품 키워드가 없습니다. 종료합니다.")
        temp_db.disconnect()
        sys.exit(1)
    target_samsung_keywords = select_from_list(samsung_keyword_list, "삼성 제품 키워드")

    # 3. 경쟁사 브랜드 선택
    comp_rows = temp_db.get_competitor_brands(product_line=product_line)
    comp_keyword_list = [row[2] for row in comp_rows]  # keyword
    if not comp_keyword_list:
        print("경쟁사 브랜드가 없습니다. 종료합니다.")
        temp_db.disconnect()
        sys.exit(1)
    target_comp_keywords = select_from_list(comp_keyword_list, "경쟁사 브랜드")

    temp_db.disconnect()

    # 4. 저장 테이블 모드
    print("\n[저장 모드]")
    print("1: 운영 테이블 저장 (market_comp_product)")
    print("2: 테스트 테이블 저장 (test_market_comp_product)")
    print("3: DRY RUN (DB 저장 안함, 로그만 출력)")
    mode_input = input("선택 (1/2/3, 기본값=1): ").strip()

    test_mode = False
    dry_run = False
    if mode_input == '2':
        test_mode = True
    elif mode_input == '3':
        test_mode = True
        dry_run = True

    # 5. 배치 ID 생성
    print("\n[배치 ID 설정]")
    print("백필용 배치 ID를 직접 입력할 수 있습니다.")
    print("빈칸으로 두면 자동으로 생성됩니다.")
    custom_batch_id = input("배치 ID: ").strip()

    if custom_batch_id:
        batch_id = custom_batch_id
    else:
        batch_prefix = "test_" if test_mode else "bf_"
        batch_id = f"{batch_prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 실행
    setup_logger()

    analyzer = CompetitorAnalyzer(
        test_mode=test_mode,
        test_product_line=product_line,
        target_samsung_keywords=target_samsung_keywords,
        target_comp_keywords=target_comp_keywords,
        dry_run=dry_run,
        batch_id=batch_id
    )
    analyzer.run()
