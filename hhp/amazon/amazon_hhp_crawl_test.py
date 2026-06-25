"""
Amazon HHP Integrated Crawler (Test)

Flow:
1. Main   - collect search result products
2. BSR    - collect Best Seller products and update/insert BSR ranks
3. Detail - crawl product detail pages
"""

import sys
import os
import argparse
import traceback
import time
from datetime import datetime
import pytz

# Common environment setup: working directory, encoding, and paths.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from common.setup import setup_environment
setup_environment(__file__)

from hhp.amazon.amazon_hhp_main import AmazonMainCrawler
from hhp.amazon.amazon_hhp_bsr import AmazonBSRCrawler
from hhp.amazon.amazon_hhp_dt import AmazonDetailCrawler
from common.base_crawler import BaseCrawler
from common.alert_hhp_monitor import send_crawl_alert


class AmazonIntegratedCrawlerTest:
    """Amazon integrated crawler for test mode."""

    def __init__(self, resume_from=None, batch_id=None):
        """
        Args:
            resume_from: stage to resume from ('main'/'bsr'/'detail'/None)
            batch_id: existing batch ID to reuse when resuming
        """
        self.account_name = 'Amazon'
        self.batch_id = batch_id
        self.start_time_kst = None
        self.start_time_server = None
        self.end_time = None
        self.resume_from = resume_from
        self.base_crawler = BaseCrawler()
        self.korea_tz = pytz.timezone('Asia/Seoul')

    def run(self):
        """Run the integrated crawler in test mode. Returns True when at least one stage succeeds."""
        self.start_time_kst = datetime.now(self.korea_tz)
        self.start_time_server = datetime.now()

        # Generate or reuse batch_id.
        if not self.batch_id:
            self.batch_id = self.base_crawler.generate_batch_id(self.account_name, test_mode=True)

        # Start logging console output to file.
        log_file = self.base_crawler.start_logging(self.batch_id)

        print("\n" + "="*60)
        print("Amazon Integrated Crawler (Test)")
        print("="*60)
        print(f"batch_id: {self.batch_id}")
        if log_file:
            print(f"log_file: {log_file}")
        if self.resume_from:
            print(f"resume_from: {self.resume_from}")

        try:
            # Results shape: {'stage': {'success': bool, 'duration': float}}
            crawl_results = {'main': None, 'bsr': None, 'detail': None}

            # STEP 1: Main
            if not self.resume_from or self.resume_from == 'main':
                print(f"\n[STEP 1/3] Main Crawler...")
                stage_start = time.time()
                try:
                    success = AmazonMainCrawler(test_mode=True, batch_id=self.batch_id).run()
                    crawl_results['main'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Main: {e}")
                    crawl_results['main'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                crawl_results['main'] = 'skipped'

            main_ok = (
                crawl_results['main'] == 'skipped'
                or (isinstance(crawl_results['main'], dict) and crawl_results['main'].get('success'))
            )

            # STEP 2: BSR - run only after Main succeeds.
            if (not self.resume_from or self.resume_from in ['main', 'bsr']) and main_ok:
                print(f"\n[STEP 2/3] BSR Crawler...")
                stage_start = time.time()
                try:
                    success = AmazonBSRCrawler(test_mode=True, batch_id=self.batch_id).run()
                    crawl_results['bsr'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] BSR: {e}")
                    crawl_results['bsr'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 2/3] BSR Crawler - SKIP because Main failed")
                crawl_results['bsr'] = 'skipped'

            bsr_ok = (
                crawl_results['bsr'] == 'skipped'
                or (isinstance(crawl_results['bsr'], dict) and crawl_results['bsr'].get('success'))
            )

            # STEP 3: Detail - run only after Main and BSR succeed.
            if (not self.resume_from or self.resume_from in ['main', 'bsr', 'detail']) and main_ok and bsr_ok:
                print(f"\n[STEP 3/3] Detail Crawler...")
                stage_start = time.time()
                try:
                    success = AmazonDetailCrawler(batch_id=self.batch_id, test_mode=True).run()
                    crawl_results['detail'] = {'success': success, 'duration': time.time() - stage_start}
                except Exception as e:
                    print(f"[ERROR] Detail: {e}")
                    crawl_results['detail'] = {'success': False, 'duration': time.time() - stage_start}
            else:
                if not main_ok:
                    print(f"\n[STEP 3/3] Detail Crawler - SKIP because Main failed")
                elif not bsr_ok:
                    print(f"\n[STEP 3/3] Detail Crawler - SKIP because BSR failed")
                crawl_results['detail'] = 'skipped'

            # Print stage results.
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds()

            print("\n" + "="*60)
            print(f"Done ({elapsed/60:.1f} min)")
            for step, result in crawl_results.items():
                if result == 'skipped':
                    status = "SKIP"
                elif isinstance(result, dict):
                    status = "OK" if result.get('success') else "FAIL"
                else:
                    status = "FAIL"
                print(f"  {step}: {status}")
            print("="*60)

            # Send alert.
            failed_stages = [
                k for k, v in crawl_results.items()
                if isinstance(v, dict) and v.get('success') is False
            ]
            send_crawl_alert(
                retailer='USA Amazon HHP',
                results=crawl_results,
                failed_stages=failed_stages,
                elapsed_time=elapsed,
                resume_from=self.resume_from,
                test_mode=True,
                start_time_kst=self.start_time_kst,
                start_time_server=self.start_time_server
            )

            # Stop logging.
            self.base_crawler.stop_logging()

            success_count = sum(
                1 for r in crawl_results.values()
                if isinstance(r, dict) and r.get('success') is True
            )
            return success_count > 0

        except Exception as e:
            print(f"\n[ERROR] Amazon HHP Integrated Crawler (Test Mode) failed: {e}")
            traceback.print_exc()

            # Send alert for fatal errors.
            self.end_time = datetime.now()
            elapsed = (self.end_time - self.start_time_server).total_seconds() if self.start_time_server else 0
            send_crawl_alert(
                retailer='USA Amazon HHP',
                results=crawl_results,
                failed_stages=['Fatal error'],
                elapsed_time=elapsed,
                error_message=str(e),
                resume_from=self.resume_from,
                test_mode=True,
                start_time_kst=self.start_time_kst,
                start_time_server=self.start_time_server
            )

            # Stop logging after fatal errors.
            self.base_crawler.stop_logging()
            return False


def main():
    """Test integrated crawler entry point."""
    parser = argparse.ArgumentParser(description='Amazon HHP Integrated Crawler (Test Mode)')
    parser.add_argument('--resume-from', type=str, choices=['main', 'bsr', 'detail'])
    parser.add_argument('--batch-id', type=str)
    args = parser.parse_args()

    if args.resume_from and not args.batch_id:
        print("[ERROR] --batch-id is required when using --resume-from")
        exit(1)

    crawler = AmazonIntegratedCrawlerTest(resume_from=args.resume_from, batch_id=args.batch_id)
    success = crawler.run()
    exit(0 if success else 1)


if __name__ == '__main__':
    main()


