"""
BestBuy TV 통합 크롤러 (운영용)

================================================================================
실행 흐름: Main → BSR → Promotion → Trend → Detail
================================================================================
STEP 1. Main      - 검색 결과 페이지에서 제품 목록 수집 (최대 300개)
STEP 2. BSR       - Best Seller 페이지에서 제품 목록 수집 (최대 100개)
STEP 3. Promotion - 프로모션 페이지에서 제품 목록 수집
STEP 4. Trend     - 트렌드 페이지에서 제품 목록 수집
STEP 5. Detail    - 수집된 모든 제품의 상세 페이지 크롤링

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
python bby_tv_crawl.py

# 특정 단계부터 재시작
python bby_tv_crawl.py --resume-from detail --batch-id b_20250123_143045
python bby_tv_crawl.py --resume-from bsr --batch-id b_20250123_143045

================================================================================
저장 테이블
================================================================================
- Main/BSR/Promotion/Trend → bby_tv_product_list (제품 목록)
- Detail                   → tv_retail_com (상세 정보 + 리뷰)
"""

import sys
import os
import argparse
import traceback
import time
from datetime import datetime
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from tv.bestbuy.bby_tv_main import BestBuyTVMainCrawler
from tv.bestbuy.bby_tv_bsr import BestBuyTVBSRCrawler
from tv.bestbuy.bby_tv_pmt import BestBuyTVPromotionCrawler
from tv.bestbuy.bby_tv_trend import BestBuyTVTrendCrawler
from tv.bestbuy.bby_tv_dt import BestBuyTVDetailCrawler
from common.base_crawler import BaseCrawler
from common.alert_hhp_monitor import send_crawl_alert

RESUME_STAGES = ['main', 'bsr', 'pmt', 'trend', 'detail']


class BestBuyTVIntegratedCrawler:
    """BestBuy TV 통합 크롤러 (운영용)"""

    def __init__(self, resume_from=None, batch_id=None):
        """
        Args:
            resume_from: 재시작 단계 ('main'/'bsr'/'pmt'/'trend'/'detail'/None)
            batch_id: 재시작 시 사용할 배치 ID
        """
        self.account_name = 'Bestbuy'
        self.batch_id = batch_id
        self.start_time_kst = None
        self.start_time_server = None
        self.end_time = None
        self.resume_from = resume_from
        self.base_crawler = BaseCrawler()
        self.korea_tz = pytz.timezone('Asia/Seoul')

    def _should_run(self, stage):
        """resume_from 기준으로 해당 단계를 실행할지 여부 반환"""
        if not self.resume_from:
            return True
        resume_idx = RESUME_STAGES.index(self.resume_from)
        stage_idx = RESUME_STAGES.index(stage)
        return stage_idx >= resume_idx

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
        print("BestBuy TV Integrated Crawler (Production)")
        print("="*60)
        print(f"batch_id: {self.batch_id}")
        if log_file:
            print(f"log_file: {log_file}")
        if self.resume_from:
            print(f"resume_from: {self.resume_from}")

        crawl_results = {'main': None, 'bsr': None, 'pmt': None, 'trend': None, 'detail': None}
        stop_pipeline = False

        try:
            # STEP 1: Main
            if self._should_run('main'):
                print(f"\n[STEP 1/5] Main Crawler...")
                stage_start = time.time()
                try:
                    success = BestBuyTVMainCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['main'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Main: {e}")
                    traceback.print_exc()
                    crawl_results['main'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['main'] = 'skipped'
            if isinstance(crawl_results['main'], dict) and crawl_results['main'].get('success') is False:
                print("[ERROR] Main Crawler failed - stopping pipeline")
                stop_pipeline = True

            # STEP 2: BSR
            if stop_pipeline:
                crawl_results['bsr'] = 'skipped'
            elif self._should_run('bsr'):
                print(f"\n[STEP 2/5] BSR Crawler...")
                stage_start = time.time()
                try:
                    success = BestBuyTVBSRCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['bsr'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] BSR: {e}")
                    traceback.print_exc()
                    crawl_results['bsr'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['bsr'] = 'skipped'
            if isinstance(crawl_results['bsr'], dict) and crawl_results['bsr'].get('success') is False:
                print("[ERROR] BSR Crawler failed - stopping pipeline")
                stop_pipeline = True

            # STEP 3: Promotion
            if stop_pipeline:
                crawl_results['pmt'] = 'skipped'
            elif self._should_run('pmt'):
                print(f"\n[STEP 3/5] Promotion Crawler...")
                stage_start = time.time()
                try:
                    success = BestBuyTVPromotionCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['pmt'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Promotion: {e}")
                    traceback.print_exc()
                    crawl_results['pmt'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['pmt'] = 'skipped'
            if isinstance(crawl_results['pmt'], dict) and crawl_results['pmt'].get('success') is False:
                print("[ERROR] Promotion Crawler failed - stopping pipeline")
                stop_pipeline = True

            # STEP 4: Trend
            if stop_pipeline:
                crawl_results['trend'] = 'skipped'
            elif self._should_run('trend'):
                print(f"\n[STEP 4/5] Trend Crawler...")
                stage_start = time.time()
                try:
                    success = BestBuyTVTrendCrawler(test_mode=False, batch_id=self.batch_id).run()
                    crawl_results['trend'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Trend: {e}")
                    traceback.print_exc()
                    crawl_results['trend'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['trend'] = 'skipped'
            if isinstance(crawl_results['trend'], dict) and crawl_results['trend'].get('success') is False:
                print("[ERROR] Trend Crawler failed - stopping pipeline")
                stop_pipeline = True

            # STEP 5: Detail
            if stop_pipeline:
                crawl_results['detail'] = 'skipped'
            elif self._should_run('detail'):
                print(f"\n[STEP 5/5] Detail Crawler...")
                stage_start = time.time()
                try:
                    success = BestBuyTVDetailCrawler(batch_id=self.batch_id, test_mode=False).run()
                    crawl_results['detail'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Detail: {e}")
                    traceback.print_exc()
                    crawl_results['detail'] = {'success': False, 'duration': time.time() - stage_start}
            else:
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
                retailer='USA BestBuy TV',
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
                retailer='USA BestBuy TV',
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
    parser = argparse.ArgumentParser(description='BestBuy TV Integrated Crawler (Production)')
    parser.add_argument('--resume-from', type=str, choices=RESUME_STAGES)
    parser.add_argument('--batch-id', type=str)
    args = parser.parse_args()

    if args.resume_from and not args.batch_id:
        print("[ERROR] --batch-id is required when using --resume-from")
        exit(1)

    crawler = BestBuyTVIntegratedCrawler(resume_from=args.resume_from, batch_id=args.batch_id)
    success = crawler.run()
    exit(0 if success else 1)


if __name__ == '__main__':
    main()
