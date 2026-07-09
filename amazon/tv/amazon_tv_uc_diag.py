"""
UC 리뷰 추출 진단 — DP는 리뷰를 뽑는데 UC만 0인 원인 규명 (측정 전용).

가설 후보:
  H1) is_review_gated 오탐: page_source에 배너 텍스트가 리뷰와 공존 → 추출 스킵
  H2) page_source 스냅샷 문제: lazy 리뷰가 라이브 DOM엔 있으나 page_source엔 없음
  H3) 실제 게이트: 배너만 있고 리뷰 컨테이너 0

판별:
  UC로 상품 N개 열고, move_to_review_section 후:
   - banner_in_source: 'account verification' 문자열 존재?
   - lxml_containers: html.fromstring(page_source) 기준 review_container 체인 매칭 수
   - live_containers: driver.find_elements(review_container xpath들) 라이브 DOM 기준 수
   - is_review_gated 결과
   - extract_reviews_from_detail_page 결과
  → banner=Y & live>0 & lxml=0  → H1(오탐) 또는 H2 조합
  → banner=Y & live=0           → H3(실제 게이트)
  → banner=N & lxml=0 & live>0  → H2(스냅샷)

사용법 (RDP):
  python amazon/tv/amazon_tv_uc_diag.py --batch-id a_20260708_170013 --count 2
"""

import sys
import os
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

import time
import config
import psycopg2
from lxml import html as lh
from selenium.webdriver.common.by import By
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


def container_chain_fields(xpaths):
    return ['review_container'] + sorted(
        k for k in xpaths if k.startswith('review_container_fallback'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch-id', required=True)
    ap.add_argument('--count', type=int, default=2)
    args = ap.parse_args()

    products = load_products(args.batch_id, args.count)
    print(f"[INFO] UC 리뷰 진단 — {len(products)}개 상품")

    c = AmazonTVDetailUC(batch_id=f'uc_diag_{datetime.now():%Y%m%d_%H%M%S}', test_mode=True)
    if not c.initialize():
        print("[ERROR] initialize 실패")
        return
    driver = c.page.driver
    cfields = container_chain_fields(c.xpaths)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uc_diag')
    os.makedirs(out_dir, exist_ok=True)

    try:
        for i, (url, name) in enumerate(products, 1):
            print("\n" + "=" * 66)
            print(f"[{i}] {(name or '')[:50]}")
            print(url)
            driver.get(url)
            time.sleep(3)
            tree0 = lh.fromstring(c.page.html)
            has_link = bool(tree0.xpath(c.xpaths['review_link']['xpath']))
            c.move_to_review_section(has_link)
            time.sleep(2)

            page_html = c.page.html
            tree = lh.fromstring(page_html)

            banner = 'account verification' in page_html.lower()

            # lxml(page_source) 기준 컨테이너 수
            lxml_counts = {}
            for f in cfields:
                xp = c.xpaths.get(f, {}).get('xpath')
                lxml_counts[f] = len(tree.xpath(xp)) if xp else 0

            # 라이브 DOM(find_elements) 기준 컨테이너 수
            live_counts = {}
            for f in cfields:
                xp = c.xpaths.get(f, {}).get('xpath')
                try:
                    live_counts[f] = len(driver.find_elements(By.XPATH, xp)) if xp else 0
                except Exception as e:
                    live_counts[f] = f'err:{str(e)[:30]}'

            gated = c.is_review_gated(page_html)
            content, count = c.extract_reviews_from_detail_page(tree, max_reviews=20)

            print(f"  banner_in_source('account verification'): {banner}")
            print(f"  is_review_gated: {gated}")
            print(f"  lxml(page_source) containers: {lxml_counts}")
            print(f"  live DOM(find_elements) containers: {live_counts}")
            print(f"  extract_reviews_from_detail_page: count={count}")
            print(f"  page_source length: {len(page_html)}")

            # 리뷰 섹션 HTML 저장
            snap = os.path.join(out_dir, f'{i}_{(name or "x")[:8].strip()}_reviewsec.html')
            secs = tree.xpath("//div[@id='customer-reviews_feature_div']")
            with open(snap, 'w', encoding='utf-8') as fp:
                fp.write(lh.tostring(secs[0], encoding='unicode') if secs else page_html[:200000])
            print(f"  snapshot: {snap}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            c.db_conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
