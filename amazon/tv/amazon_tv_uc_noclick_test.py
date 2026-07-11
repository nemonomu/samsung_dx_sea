"""
리뷰링크 클릭 격리 테스트 — full crawl_detail에서 리뷰링크 클릭만 scrollIntoView로
교체했을 때 게이트가 사라지는지 측정 (dry-run, DB 미기록).

배경:
  같은 쉰 신원·같은 배치에서
    - dt_update_uc(최소 흐름: 클릭 없음, scrollIntoView)      → 202/250 게이트 0
    - full crawl_detail(스펙버튼 클릭 + 리뷰링크 클릭)         → 20/20 게이트 (DP·UC 공통)
  차이는 "무거운 상호작용". 유력 용의자는 move_to_review_section의 리뷰링크 클릭
  (acrCustomerReviewLink.click()). 이 클릭만 scrollIntoView로 바꿔 격리한다.

방식:
  AmazonTVDetailUC(UC + full crawl_detail) 를 상속하되 move_to_review_section만
  "클릭 없이 scrollIntoView" 로 오버라이드. 나머지(ZIP팝업·스펙버튼 2클릭·추출)는
  그대로. crawl_detail은 read-only라 호출만으로 DB 미기록(dry-run).

판정:
  게이트 ≈ 0  → 리뷰링크 클릭이 범인. detail을 이 방식으로 바꾸면 한 페이지에서
                spec+review 다 수집 (분리 불필요).
  게이트 지속 → 클릭이 원인 아님(스펙버튼/ZIP팝업 등 다른 상호작용).

사용법 (RDP):
  python amazon/tv/amazon_tv_uc_noclick_test.py            # batch 선택, 전체 row
  python amazon/tv/amazon_tv_uc_noclick_test.py --limit 50 # 개수 제한
"""

import sys
import os
import json
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
from amazon.tv.amazon_tv_uc import AmazonTVDetailUC
from common.base_crawler import BaseCrawler


class AmazonTVDetailUCNoClick(AmazonTVDetailUC):
    """full crawl_detail 그대로 + move_to_review_section만 '클릭 없이 스크롤'로 교체."""

    def move_to_review_section(self, has_review_link):
        """리뷰링크 클릭 없이 reviewsMedley로 scrollIntoView만 (dt_update_uc 방식)."""
        try:
            self.page.run_js(
                "var e=document.getElementById('reviewsMedley')"
                "||document.getElementById('customer-reviews_feature_div');"
                "if(e)e.scrollIntoView({block:'center',behavior:'instant'});")
        except Exception:
            pass
        time.sleep(1)
        return True


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def select_batch_id():
    ids = BaseCrawler.fetch_today_batch_ids(
        table_name='tv_retail_com', account_name='Amazon', test_mode=False)
    if ids:
        print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id 목록:")
        for i, bid in enumerate(ids, 1):
            print(f"  {i}. {bid}")
        print("  0. 직접 입력")
        choice = input("\n번호 선택 ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ids):
            return ids[int(choice) - 1]
        if choice == '0':
            return input("batch_id 입력: ").strip()
        if choice and not choice.isdigit():
            return choice
    else:
        print(f"오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id가 없습니다.")
    return input("batch_id 직접 입력: ").strip()


def load_products(batch_id, limit):
    """배치 전체 row (product_url 있는) — id 순."""
    conn = db_connect()
    cur = conn.cursor()
    q = """
        SELECT id, retailer_sku_name, product_url, page_type, main_rank, bsr_rank, calendar_week
        FROM tv_retail_com
        WHERE account_name='Amazon' AND batch_id=%s AND product_url IS NOT NULL
        ORDER BY id
    """
    if limit and limit > 0:
        q += f" LIMIT {int(limit)}"
    cur.execute(q, (batch_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'account_name': 'Amazon', 'retailer_sku_name': r[1], 'product_url': r[2],
        'page_type': r[3], 'main_rank': r[4], 'bsr_rank': r[5], 'calendar_week': r[6],
    } for r in rows]


def main():
    ap = argparse.ArgumentParser(description='리뷰링크 클릭 격리 테스트 (dry-run)')
    ap.add_argument('--batch-id', help='대상 batch_id (미지정 시 오늘 배치 목록에서 선택)')
    ap.add_argument('--limit', type=int, default=0, help='대상 개수 제한 (0=전체)')
    ap.add_argument('--sleep', type=float, default=2.5, help='상품 간 대기(초)')
    args = ap.parse_args()

    batch_id = args.batch_id or select_batch_id()
    if not batch_id:
        print("[ERROR] batch_id가 필요합니다.")
        return

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    products = load_products(batch_id, args.limit)
    if not products:
        print("[ERROR] 대상 상품 없음")
        return
    print(f"[INFO] 리뷰링크 클릭 격리 테스트 (dry-run) — batch={batch_id}, "
          f"상품 {len(products)}개 (move_to_review_section = scrollIntoView, 클릭 없음)")

    crawler = AmazonTVDetailUCNoClick(batch_id=f'noclick_{ts}', test_mode=True)
    if not crawler.initialize():
        print("[ERROR] initialize 실패")
        return

    results = {'batch_id': batch_id, 'started_at': ts, 'mode': 'noclick', 'products': []}
    ok = gated = noreview = err = 0
    first_gate_at = None
    prev_gcnt = 0
    try:
        for i, p in enumerate(products, 1):
            entry = {'seq': i, 'product_url': p['product_url']}
            try:
                data = crawler.crawl_detail(dict(p))  # read-only → dry-run
                rev = data.get('detailed_review_content') if isinstance(data, dict) else None
                entry['reviews'] = bool(rev)
                # 게이트 여부: detail_report 누적 카운트의 직전 대비 증가분으로 판별
                gcnt = crawler.detail_report.get('review_gated_count', 0)
                gated_this = gcnt - prev_gcnt
                prev_gcnt = gcnt
                is_gated_now = False
                if gated_this > 0:
                    gated += 1
                    is_gated_now = True
                    if first_gate_at is None:
                        first_gate_at = i
                elif rev:
                    ok += 1
                else:
                    noreview += 1
                entry.update({'gated': is_gated_now})
                print(f"[{i}/{len(products)}] gated={is_gated_now} reviews={'Y' if rev else 'N'} "
                      f"(누적 ok={ok} gated={gated} noreview={noreview})")
            except Exception as e:
                err += 1
                entry['error'] = str(e)[:150]
                print(f"[{i}/{len(products)}] ERROR: {e}")
            results['products'].append(entry)
            time.sleep(args.sleep)
    finally:
        results['summary'] = {'total': len(products), 'ok': ok, 'gated': gated,
                              'noreview': noreview, 'error': err, 'first_gate_at': first_gate_at}
        # 25개 버킷 곡선
        buckets = {}
        for pr in results['products']:
            b = (pr['seq'] - 1) // 25
            d = buckets.setdefault(b, {'n': 0, 'ok': 0, 'gated': 0})
            d['n'] += 1
            if pr.get('gated'):
                d['gated'] += 1
            elif pr.get('reviews'):
                d['ok'] += 1
        results['summary']['buckets'] = buckets
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uc_noclick')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'{batch_id}_{ts}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("\n===== 리뷰링크 클릭 격리 테스트 요약 (dry-run) =====")
        print(f"대상 {len(products)} | 리뷰추출 {ok} | 게이트 {gated} | 무리뷰 {noreview} | "
              f"에러 {err} | 첫 게이트 순번: {first_gate_at}")
        print("소진 곡선 (25개 버킷): 순번 | ok | gated")
        for b in sorted(buckets):
            v = buckets[b]
            print(f"  {b*25+1:3d}~{b*25+v['n']:3d} | ok={v['ok']:2d} | gated={v['gated']:2d}")
        print("판정: 게이트≈0 → 리뷰링크 클릭이 범인(한 패스 해결 가능) / 지속 → 다른 상호작용")
        print(f"결과: {out_path}")
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


if __name__ == '__main__':
    main()
