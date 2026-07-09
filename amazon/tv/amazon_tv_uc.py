"""
Amazon TV 크롤러 — UC(undetected-chromedriver) 서브클래스 (main/BSR/detail).

배경:
  amazon.com은 (.in/.de엔 없는) 세션 신뢰점수 기반 리뷰 게이트를 도입했고,
  DrissionPage(CDP) 세션은 고속 리뷰 요청으로 점수를 소진해 게이트를 자초한다
  (2026-07-08 detail: ~130개째 전멸). UC는 자동화 흔적을 지워 소진이 없다
  (동일 배치 145/145 게이트 0 실측). 그래서 브라우저를 UC로 옮긴다.

설계 (어댑터 기반 — 재작성 최소화):
  - self.page 를 UCPage(uc_page.py) 로 교체하면, 검증된 크롤/복구/추출 로직
    (setup 이후의 18개 self.page-의존 메서드 포함)이 코드 재작성 없이 UC 위에서 돈다.
  - UCBrowserMixin.setup_browser() 만 오버라이드하면 main/BSR/detail 모두 전환된다.
    crawl_page / crawl_detail / recover / zip / review-nav 전부 부모 그대로.
  - lxml 추출 헬퍼 + DB 저장(save_products/save_to_retail_com/upsert_item_mst)은
    브라우저 무관 → 그대로 재사용.

격리:
  - 신규 파일. common/ 공유 base 및 Walmart/HHP/기존 DrissionPage 경로 0줄 변경.
  - 검증 완료 후 오케스트레이터가 이 UC 크롤러를 호출하도록 스위치.
"""

import sys
import os
import traceback

_project_root = os.path.abspath(os.path.dirname(__file__))
while _project_root and not os.path.exists(os.path.join(_project_root, 'common', 'setup.py')):
    _parent = os.path.dirname(_project_root)
    if _parent == _project_root:
        break
    _project_root = _parent
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from common.setup import setup_environment
setup_environment(__file__)

from amazon.tv.uc_page import UCPage, make_uc_driver
from amazon.tv.amazon_tv_main import AmazonTVMainCrawler
from amazon.tv.amazon_tv_bsr import AmazonTVBSRCrawler
from amazon.tv.amazon_tv_dt import (
    AmazonTVDetailCrawler, refresh_trusted_profile, TRUSTED_PROFILE_DIR,
)


class UCBrowserMixin:
    """setup_browser() 만 UC로 교체. 나머지 메서드는 부모 그대로 self.page(UCPage) 사용.

    self.uc_use_trusted_profile: 신뢰 프로필 사용 여부 (기본 True).
      - detail 은 initialize()가 이미 refresh + browser_user_data_dir 설정 → 그대로 사용
      - main/BSR 은 여기서 refresh 후 사용
    """
    uc_use_trusted_profile = True

    def setup_browser(self):
        try:
            print("[INFO] UC(undetected-chromedriver) 설정 중...")
            user_data_dir = getattr(self, 'browser_user_data_dir', None)
            if user_data_dir is None and self.uc_use_trusted_profile:
                # detail 외 스테이지(main/BSR)를 위한 신뢰 프로필 준비
                refresh_trusted_profile()
                cookies_path = os.path.join(
                    TRUSTED_PROFILE_DIR, 'Default', 'Network', 'Cookies')
                if os.path.exists(cookies_path):
                    user_data_dir = TRUSTED_PROFILE_DIR

            self.page = UCPage(make_uc_driver(user_data_dir=user_data_dir))
            self.page.set.window.max()
            print("[OK] UC 설정 완료"
                  + (f" (신뢰 프로필: {user_data_dir})" if user_data_dir else " (기본 프로필)"))

            zip_code = getattr(self, 'amazon_zip_code', '10001')
            if not self.set_amazon_zip_code(zip_code):
                print("[ERROR] Amazon ZIP code 설정 실패")
                return False
            return True
        except Exception as e:
            print(f"[ERROR] UC setup_browser 실패: {e}")
            traceback.print_exc()
            return False


class AmazonTVMainUC(UCBrowserMixin, AmazonTVMainCrawler):
    """UC 기반 Main 크롤러 — 리스팅(검색 결과) 수집."""
    pass


class AmazonTVBSRUC(UCBrowserMixin, AmazonTVBSRCrawler):
    """UC 기반 BSR 크롤러 — 베스트셀러 수집."""
    pass


class AmazonTVDetailUC(UCBrowserMixin, AmazonTVDetailCrawler):
    """UC 기반 Detail 크롤러 — 상세/리뷰 수집 (게이트 소진 없음)."""
    pass


def main():
    """단독 실행 진입점 — 스테이지 지정 테스트/수동 실행용.

    사용법:
      python amazon/tv/amazon_tv_uc.py --stage detail --batch-id a_xxx
      python amazon/tv/amazon_tv_uc.py --stage main
    """
    import argparse
    ap = argparse.ArgumentParser(description='Amazon TV UC 크롤러 (단독 실행)')
    ap.add_argument('--stage', required=True, choices=['main', 'bsr', 'detail'])
    ap.add_argument('--batch-id', type=str)
    ap.add_argument('--test', action='store_true', help='test 테이블 사용')
    args = ap.parse_args()

    test_mode = args.test
    if args.stage == 'main':
        crawler = AmazonTVMainUC(test_mode=test_mode, batch_id=args.batch_id)
    elif args.stage == 'bsr':
        crawler = AmazonTVBSRUC(test_mode=test_mode, batch_id=args.batch_id)
    else:
        crawler = AmazonTVDetailUC(batch_id=args.batch_id, test_mode=test_mode)
    ok = crawler.run()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
