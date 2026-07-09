"""
UC 리뷰 게이트 트리거 실험 — 리뷰링크 '클릭' vs '스크롤만' (측정 전용).

배경:
  같은 시각 UC만 게이트(수동/DP는 리뷰 보임) → 신원/IP 아님, UC 자동화 세션 감지.
  아침 dt_update_uc(145/145)는 리뷰링크 클릭 없이 scrollIntoView 로 접근했고,
  지금 DetailUC(crawl_detail)는 move_to_review_section 이 acrCustomerReviewLink 를
  클릭한다. 클릭이 게이트 트리거인지 검증.

방식:
  상품마다 UC로 2번 로드해서 각각:
   A) 클릭 방식  : move_to_review_section(has_link=True) → 컨테이너 수
   B) 스크롤 방식: 리뷰링크 클릭 없이 scrollIntoView(reviewsMedley) + scroll_to_bottom
                   → 컨테이너 수
  A<B(스크롤만 성공) 이면 클릭이 트리거 → DetailUC 는 move_to_review_section 을
  스크롤 방식으로 오버라이드하면 해결.

사용법 (RDP):
  python amazon/tv/amazon_tv_uc_diag2.py --batch-id a_20260708_170013 --count 3
"""

import sys
import os
import time
import argparse
from datetime import datetime

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

import config
import psycopg2
from lxml import html as lh
from amazon.tv.amazon_tv_uc import AmazonTVDetailUC


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_products(batch_id, count):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT product_url, retailer_sku_name FROM tv_retail_com
        WHERE account_name='Amazon' AND batch_id=%s AND product_url IS NOT NULL
        ORDER BY id LIMIT %s
    """, (batch_id, count))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def count_reviews(crawler):
    """현재 페이지에서 리뷰 컨테이너 체인 매칭 수 + 게이트 여부."""
    page_html = crawler.page.html
    tree = lh.fromstring(page_html)
    containers, matched = crawler.safe_extract_chain_list(tree, 'review_container')
    gated = 'account verification' in page_html.lower()
    content, count = crawler.extract_reviews_from_detail_page(tree, max_reviews=20)
    return {'containers': len(containers), 'matched': matched, 'gated': gated, 'extracted': count}


def scroll_only_load(crawler):
    """리뷰링크 클릭 없이 scrollIntoView + scroll_to_bottom (dt_update_uc 방식)."""
    try:
        crawler.page.run_js(
            "var e=document.getElementById('reviewsMedley')"
            "||document.getElementById('customer-reviews_feature_div');"
            "if(e)e.scrollIntoView({block:'center',behavior:'instant'});")
    except Exception:
        pass
    time.sleep(3)
    try:
        crawler.page.run_js("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch-id', required=True)
    ap.add_argument('--count', type=int, default=3)
    args = ap.parse_args()

    products = load_products(args.batch_id, args.count)
    print(f"[INFO] 클릭 vs 스크롤 트리거 실험 — {len(products)}개 상품")

    c = AmazonTVDetailUC(batch_id=f'uc_diag2_{datetime.now():%Y%m%d_%H%M%S}', test_mode=True)
    if not c.initialize():
        print("[ERROR] initialize 실패")
        return
    try:
        for i, (url, name) in enumerate(products, 1):
            print("\n" + "=" * 60)
            print(f"[{i}] {(name or '')[:45]}")

            # A) 클릭 방식
            c.page.get(url)
            time.sleep(3)
            tree0 = lh.fromstring(c.page.html)
            has_link = bool(tree0.xpath(c.xpaths['review_link']['xpath']))
            c.move_to_review_section(has_link)
            time.sleep(2)
            a = count_reviews(c)
            print(f"  A) 클릭 방식  : containers={a['containers']} extracted={a['extracted']} "
                  f"gated={a['gated']} matched={a['matched']}")

            # B) 스크롤만 방식 (새로 로드해서 클릭 없이)
            c.page.get(url)
            time.sleep(3)
            scroll_only_load(c)
            b = count_reviews(c)
            print(f"  B) 스크롤 방식: containers={b['containers']} extracted={b['extracted']} "
                  f"gated={b['gated']} matched={b['matched']}")

            verdict = ('클릭이 트리거(B>A)' if b['extracted'] > a['extracted']
                       else '클릭 무관(동일)' if b['extracted'] == a['extracted']
                       else 'A>B(예상외)')
            print(f"  → 판정: {verdict}")
            time.sleep(2)
    finally:
        try:
            c.page.quit()
        except Exception:
            pass
        try:
            c.db_conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
