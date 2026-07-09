"""
UC vs DrissionPage 필드 패리티 검증 (측정 전용, DB 미기록) — RDP 실행.

목적:
  UCPage 어댑터 위에서 검증된 crawl_detail 이 DrissionPage와 "동일하게" 12개
  추출 필드를 뽑는지 상품별로 대조하고, 동시에 UC가 리뷰 게이트를 통과하는지
  (리뷰 non-null) 확인한다. 어댑터 방식의 안정성/무손상을 실증하는 관문.

방식:
  같은 상품 N개에 대해
    1) DrissionPage AmazonTVDetailCrawler.crawl_detail() → 12필드
    2) UC        AmazonTVDetailUC.crawl_detail()        → 12필드
  를 각각 수집(브라우저 순차, DB 미기록)하고 필드별 일치/불일치를 출력.

  가격/별점 등은 실행 시각차로 미세하게 바뀔 수 있으니 불일치는 참고용.
  핵심 관전: (a) item/sku/screen_size/model_year 등 안정 필드 일치,
            (b) detailed_review_content — UC는 채우고 DrissionPage는 게이트일 수 있음.

사용법 (RDP):
  python amazon/tv/amazon_tv_uc_parity.py --batch-id a_20260708_170013 --count 5
"""

import sys
import os
import json
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
from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler
from amazon.tv.amazon_tv_uc import AmazonTVDetailUC

FIELDS = AmazonTVDetailCrawler.EXTRACTED_FIELDS


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_products(batch_id, count):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, retailer_sku_name, product_url, page_type, main_rank, bsr_rank, calendar_week
        FROM tv_retail_com
        WHERE account_name='Amazon' AND batch_id=%s AND product_url IS NOT NULL
        ORDER BY id LIMIT %s
    """, (batch_id, count))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'account_name': 'Amazon', 'retailer_sku_name': r[1], 'product_url': r[2],
        'page_type': r[3], 'main_rank': r[4], 'bsr_rank': r[5], 'calendar_week': r[6],
    } for r in rows]


def crawl_all(crawler, products, label):
    """crawler.initialize() 후 각 상품 crawl_detail → {field: value} 리스트 (DB 미기록)."""
    print(f"\n===== {label} 크롤링 시작 ({len(products)}개) =====")
    results = []
    if not crawler.initialize():
        print(f"[ERROR] {label} initialize 실패")
        return results
    try:
        for i, p in enumerate(products, 1):
            row = {'product_url': p['product_url']}
            try:
                data = crawler.crawl_detail(dict(p))
                if data and data is not p:
                    for f in FIELDS:
                        row[f] = data.get(f)
                    row['_ok'] = True
                else:
                    row['_ok'] = False
                    row['_note'] = data.get('_detail_skip') if isinstance(data, dict) else 'no_data'
            except Exception as e:
                row['_ok'] = False
                row['_note'] = f'exc:{str(e)[:80]}'
            results.append(row)
            rev = row.get('detailed_review_content')
            print(f"  [{i}/{len(products)}] ok={row.get('_ok')} "
                  f"reviews={'Y' if rev else 'N'} "
                  f"price={row.get('final_sku_price')} star={row.get('star_rating')}")
    finally:
        try:
            if crawler.page:
                crawler.page.quit()
        except Exception:
            pass
        try:
            if crawler.db_conn:
                crawler.db_conn.close()
        except Exception:
            pass
    return results


def main():
    ap = argparse.ArgumentParser(description='UC vs DrissionPage 필드 패리티 검증')
    ap.add_argument('--batch-id', required=True)
    ap.add_argument('--count', type=int, default=5)
    args = ap.parse_args()

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    products = load_products(args.batch_id, args.count)
    if not products:
        print("[ERROR] 대상 상품 없음")
        return
    print(f"[INFO] 패리티 검증 — batch={args.batch_id}, 상품 {len(products)}개")

    # DrissionPage 먼저 → 종료 후 UC (프로필 락 충돌 방지: 순차)
    dp = crawl_all(AmazonTVDetailCrawler(batch_id=f'parity_dp_{ts}', test_mode=True), products, 'DrissionPage')
    uc = crawl_all(AmazonTVDetailUC(batch_id=f'parity_uc_{ts}', test_mode=True), products, 'UC')

    # 대조
    print("\n" + "=" * 70)
    print("필드 패리티 대조 (DrissionPage vs UC)")
    print("=" * 70)
    stable_fields = ['item', 'screen_size', 'model_year']  # 시각차 무관 안정 필드
    volatile_fields = ['final_sku_price', 'original_sku_price', 'star_rating',
                       'count_of_star_ratings', 'sku_popularity']
    review_fields = ['summarized_review_content', 'detailed_review_content']

    summary = {'batch_id': args.batch_id, 'count': len(products), 'rows': []}
    dp_rev = uc_rev = 0
    stable_mismatch = 0
    for i, (a, b) in enumerate(zip(dp, uc), 1):
        url = a.get('product_url', '')[:60]
        print(f"\n[{i}] {url}")
        rowlog = {'url': a.get('product_url')}
        for f in stable_fields:
            av, bv = a.get(f), b.get(f)
            mark = 'OK' if av == bv else '*** MISMATCH ***'
            if av != bv:
                stable_mismatch += 1
            print(f"   {f:28s} DP={av!r:20} UC={bv!r:20} {mark}")
            rowlog[f] = {'dp': av, 'uc': bv, 'match': av == bv}
        for f in volatile_fields:
            av, bv = a.get(f), b.get(f)
            print(f"   {f:28s} DP={av!r:20} UC={bv!r:20} {'=' if av == bv else '(diff, 시각차 가능)'}")
            rowlog[f] = {'dp': av, 'uc': bv}
        for f in review_fields:
            av = bool(a.get(f))
            bv = bool(b.get(f))
            print(f"   {f:28s} DP={'Y' if av else 'N'}  UC={'Y' if bv else 'N'}")
            rowlog[f] = {'dp': av, 'uc': bv}
        if a.get('detailed_review_content'):
            dp_rev += 1
        if b.get('detailed_review_content'):
            uc_rev += 1
        summary['rows'].append(rowlog)

    print("\n" + "=" * 70)
    print("요약")
    print("=" * 70)
    print(f"안정필드(item/screen_size/model_year) 불일치: {stable_mismatch}건 "
          f"(0이어야 어댑터 정상)")
    print(f"리뷰 수집: DrissionPage {dp_rev}/{len(products)} | UC {uc_rev}/{len(products)}")
    print(f"  → UC가 DrissionPage보다 리뷰를 더 채우면 게이트 우회 실증")

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uc_parity')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{args.batch_id}_{ts}.json')
    summary['stable_mismatch'] = stable_mismatch
    summary['review'] = {'dp': dp_rev, 'uc': uc_rev}
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"결과: {out_path}")


if __name__ == '__main__':
    main()
