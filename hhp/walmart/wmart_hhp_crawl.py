"""
Walmart HHP 통합 크롤러 (운영용)

================================================================================
실행 흐름: Main → BSR → Detail
================================================================================
STEP 1. Main   - 검색 결과 페이지에서 제품 목록 수집 (최대 300개)
STEP 2. BSR    - Best Seller 페이지에서 제품 목록 수집 (최대 100개)
STEP 3. Detail - 수집된 모든 제품의 상세 페이지 크롤링

================================================================================
주요 특징
================================================================================
- 동일한 batch_id로 전체 파이프라인 실행
- 각 크롤러 실패 시에도 다음 단계 계속 진행
- --resume-from 옵션으로 특정 단계부터 재개 가능

================================================================================
사용법
================================================================================
# 처음부터 실행
python wmart_hhp_crawl.py

# 특정 단계부터 재시작
python wmart_hhp_crawl.py --resume-from detail --batch-id w_20250123_143045
python wmart_hhp_crawl.py --resume-from bsr --batch-id w_20250123_143045

================================================================================
저장 테이블
================================================================================
- Main/BSR     → wmart_hhp_product_list (제품 목록)
- Detail       → hhp_retail_com (상세 정보 + 리뷰)
"""

import sys
import os
import argparse
import traceback
import time
from datetime import datetime
import pytz

# 공통 환경 설정 (작업 디렉토리, 한글 출력, 경로 설정)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from hhp.walmart.wmart_hhp_main import WalmartMainCrawler
from hhp.walmart.wmart_hhp_bsr import WalmartBSRCrawler
from hhp.walmart.wmart_hhp_dt import WalmartDetailCrawler
from common.base_crawler import BaseCrawler
from common.alert_hhp_monitor import send_crawl_alert


class WalmartIntegratedCrawler:
    """Walmart 통합 크롤러 (운영용)"""

    def __init__(self, resume_from=None, batch_id=None):
        """
        Args:
            resume_from: 재시작 단계 ('main'/'bsr'/'detail'/None)
            batch_id: 재시작 시 사용할 배치 ID
        """
        self.account_name = 'Walmart'
        self.batch_id = batch_id
        self.start_time_kst = None
        self.start_time_server = None
        self.end_time = None
        self.resume_from = resume_from
        self.base_crawler = BaseCrawler()
        self.korea_tz = pytz.timezone('Asia/Seoul')

    def run(self):
        """통합 크롤러 실행. Returns: bool"""
        self.start_time_kst = datetime.now(self.korea_tz)
        self.start_time_server = datetime.now()

        # batch_id 생성 또는 재사용
        if not self.batch_id:
            self.batch_id = self.base_crawler.generate_batch_id(self.account_name)

        # 로깅 시작 (콘솔 출력을 파일에도 저장)
        log_file = self.base_crawler.start_logging(self.batch_id)

        print("\n" + "="*60)
        print("Walmart Integrated Crawler (Production)")
        print("="*60)
        print(f"batch_id: {self.batch_id}")
        if log_file:
            print(f"log_file: {log_file}")
        if self.resume_from:
            print(f"resume_from: {self.resume_from}")

        try:
            # 결과: {'stage': {'success': bool, 'duration': float}} 형태로 저장
            crawl_results = {'main': None, 'bsr': None, 'detail': None}

            # STEP 1: Main
            if not self.resume_from or self.resume_from == 'main':
                print(f"\n[STEP 1/3] Main Crawler...")
                stage_start = time.time()
                try:
                    success = WalmartMainCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['main'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Main: {e}")
                    traceback.print_exc()
                    crawl_results['main'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['main'] = 'skipped'

            main_ok = (
                crawl_results['main'] == 'skipped'
                or (isinstance(crawl_results['main'], dict) and crawl_results['main'].get('success'))
            )

            # STEP 2: BSR
            if (not self.resume_from or self.resume_from in ['main', 'bsr']) and main_ok:
                print(f"\n[STEP 2/3] BSR Crawler...")
                stage_start = time.time()
                try:
                    success = WalmartBSRCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['bsr'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] BSR: {e}")
                    traceback.print_exc()
                    crawl_results['bsr'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 2/3] BSR Crawler - Main 실패로 SKIP")
                crawl_results['bsr'] = 'skipped'

            bsr_ok = (
                crawl_results['bsr'] == 'skipped'
                or (isinstance(crawl_results['bsr'], dict) and crawl_results['bsr'].get('success'))
            )

            # STEP 3: Detail
            if main_ok and bsr_ok:
                print(f"\n[STEP 3/3] Detail Crawler...")
                stage_start = time.time()
                try:
                    success = WalmartDetailCrawler(batch_id=self.batch_id, test_mode=False).run()
                    crawl_results['detail'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Detail: {e}")
                    traceback.print_exc()
                    crawl_results['detail'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 3/3] Detail Crawler - Main 실패로 SKIP")
                elif not bsr_ok:
                    print(f"\n[STEP 3/3] Detail Crawler - BSR 실패로 SKIP")
                crawl_results['detail'] = 'skipped'

            # 결과 출력
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds()

            print("\n" + "="*60)
            print(f"완료 ({elapsed/60:.1f}분)")
            for step, result in crawl_results.items():
                if result == 'skipped':
                    status = "SKIP"
                elif isinstance(result, dict):
                    status = "OK" if result.get('success') else "FAIL"
                else:
                    status = "FAIL"
                print(f"  {step}: {status}")
            print("="*60)

            # 이메일 알림 발송
            failed_stages = [
                k for k, v in crawl_results.items()
                if isinstance(v, dict) and v.get('success') is False
            ]
            send_crawl_alert(
                retailer='USA Walmart HHP',
                results=crawl_results,
                failed_stages=failed_stages,
                elapsed_time=elapsed,
                resume_from=self.resume_from,
                test_mode=False,
                start_time_kst=self.start_time_kst,
                start_time_server=self.start_time_server
            )

            # 로깅 종료
            self.base_crawler.stop_logging()

            success_count = sum(
                1 for r in crawl_results.values()
                if isinstance(r, dict) and r.get('success') is True
            )
            return success_count > 0

        except Exception as e:
            print(f"\n[ERROR] Integrated crawler failed: {e}")
            traceback.print_exc()

            # 예외 발생 시에도 이메일 알림 발송
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds() if self.start_time_server else 0
            send_crawl_alert(
                retailer='USA Walmart HHP',
                results=crawl_results,
                failed_stages=['Fatal error'],
                elapsed_time=elapsed,
                error_message=str(e),
                resume_from=self.resume_from,
                test_mode=False,
                start_time_kst=self.start_time_kst,
                start_time_server=self.start_time_server
            )

            # 예외 발생 시에도 로깅 종료
            self.base_crawler.stop_logging()
            return False


def main():
    """운영용 통합 크롤러 진입점"""
    parser = argparse.ArgumentParser(description='Walmart HHP Integrated Crawler (Production)')
    parser.add_argument('--resume-from', type=str, choices=['main', 'bsr', 'detail'])
    parser.add_argument('--batch-id', type=str)
    args = parser.parse_args()

    if args.resume_from and not args.batch_id:
        print("[ERROR] --batch-id is required when using --resume-from")
        exit(1)

    crawler = WalmartIntegratedCrawler(resume_from=args.resume_from, batch_id=args.batch_id)
    success = crawler.run()
    exit(0 if success else 1)


if __name__ == '__main__':
    main()
