"""
Amazon TV 리뷰 DOM 배치 테스트 (10개 상품, 읽기 전용)

detailed_review_content 전체 NULL 원인 검증용 진단 스크립트.
- 운영 크롤러(AmazonTVDetailCrawler)와 동일한 XPath/브라우저/추출 흐름으로
  최신 배치 상품 10개의 리뷰 섹션 DOM을 검사한다.
- DB에는 아무것도 저장하지 않는다 (product_list 조회만).
- 결과는 amazon/tv/data/review_dom_test/<timestamp>/ 폴더에 저장:
    - summary.txt      : 상품별 매칭 결과 요약 (사람이 읽는 용)
    - results.json     : 상품별 상세 진단 (기계 판독용)
    - <n>_<asin>.jpg   : 리뷰 섹션 이동 직후 화면 캡처
    - <n>_<asin>_review_section.html : 리뷰 feature div의 outerHTML

검증 포인트:
1. 기존 xpath(//div[@data-hook='reviewContainer'])가 매칭되는가
2. 매칭 실패 시 리뷰 섹션에 실제로 어떤 data-hook들이 렌더링되는가
   (신규 lazy-widget 레이아웃 여부)
3. 크롤러 타이밍(클릭+1초) vs 추가 대기(+5초) 차이가 있는가
4. RDP 세션 환경(SESSIONNAME, 해상도)이 어떤가

사용법 (RDP에서):
    python amazon/tv/amazon_tv_review_dom_test.py
"""

import sys
import os
import json
import time
import traceback
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

from lxml import html as lhtml

from amazon.tv.amazon_tv_dt import AmazonTVDetailCrawler

TEST_COUNT = 10

# 신규/구 레이아웃 판별용 마커 (raw HTML substring 카운트)
HTML_MARKERS = [
    'data-hook="reviewContainer"',
    'data-hook="review"',
    'id="customerReviews"',
    'customer-reviews_feature_div',
    'reviewRichContentContainer',
    'single-review-mobile-overlay',
    'lazy-widget',
    'cr-lazy',
]


def hook_inventory(tree):
    """리뷰 feature div 내부의 data-hook 값 분포를 반환한다."""
    hooks = {}
    sections = tree.xpath("//div[@id='customer-reviews_feature_div']")
    if not sections:
        return None
    for el in sections[0].xpath('.//*[@data-hook]'):
        v = el.get('data-hook')
        hooks[v] = hooks.get(v, 0) + 1
    return hooks


def diagnose(crawler, label):
    """현재 페이지를 파싱해 리뷰 xpath 매칭 상태를 진단한다."""
    page_html = crawler.page.html
    tree = lhtml.fromstring(page_html)

    containers, matched_field = crawler.safe_extract_chain_list(tree, 'review_container')
    content, count = crawler.extract_reviews_from_detail_page(tree, max_reviews=20)

    return {
        'label': label,
        'matched_field': matched_field,
        'container_count': len(containers),
        'extracted_review_count': count,
        'first_review_preview': (content or '')[:150],
        'hook_inventory': hook_inventory(tree),
        'html_markers': {m: page_html.count(m) for m in HTML_MARKERS},
    }, tree, page_html


def main():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'data', 'review_dom_test', ts)
    os.makedirs(out_dir, exist_ok=True)

    lines = []

    def log(msg=''):
        print(msg)
        lines.append(str(msg))

    log('=' * 70)
    log('Amazon TV 리뷰 DOM 배치 테스트 (읽기 전용)')
    log(f'결과 폴더: {out_dir}')
    log('=' * 70)

    # 환경 정보 (RDP 가설 검증용)
    log(f"SESSIONNAME: {os.environ.get('SESSIONNAME', '(없음)')}")
    log(f"COMPUTERNAME: {os.environ.get('COMPUTERNAME', '(없음)')}")

    crawler = AmazonTVDetailCrawler(batch_id=f'review_dom_test_{ts}', test_mode=True)
    results = {'started_at': ts,
               'sessionname': os.environ.get('SESSIONNAME'),
               'computername': os.environ.get('COMPUTERNAME'),
               'products': []}

    try:
        if not crawler.connect_db():
            log('[ERROR] DB 연결 실패')
            return False
        if not crawler.load_xpaths('Amazon', 'detail', 'SEA', 'TV'):
            log('[ERROR] XPath 로드 실패')
            return False

        review_fields = {k: v['xpath'] for k, v in crawler.xpaths.items()
                         if k.startswith('review')}
        log('\n[로드된 review 관련 XPath]')
        for k, v in sorted(review_fields.items()):
            log(f'  {k}: {v}')
        results['review_xpaths'] = review_fields

        # 최신 배치에서 상품 10개 조회 (읽기 전용)
        cursor = crawler.db_conn.cursor()
        # 't_' 테스트 배치가 알파벳순으로 'a_' 뒤에 오므로 운영 배치만 대상
        cursor.execute("""
            SELECT batch_id FROM amazon_tv_product_list
            WHERE account_name = 'Amazon' AND batch_id LIKE 'a\\_%'
            ORDER BY batch_id DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            log('[ERROR] amazon_tv_product_list에 배치가 없음')
            return False
        source_batch = row[0]
        cursor.execute("""
            SELECT DISTINCT product_url, retailer_sku_name
            FROM amazon_tv_product_list
            WHERE account_name = 'Amazon' AND batch_id = %s
              AND product_url IS NOT NULL
            ORDER BY product_url LIMIT %s
        """, (source_batch, TEST_COUNT))
        products = cursor.fetchall()
        cursor.close()
        log(f'\n[INFO] 소스 배치: {source_batch}, 테스트 상품 수: {len(products)}')
        results['source_batch'] = source_batch

        if not crawler.setup_browser():
            log('[ERROR] 브라우저 설정 실패')
            return False

        # 브라우저 쪽 화면 정보
        try:
            screen_info = crawler.page.run_js(
                'return JSON.stringify({screenW: screen.width, screenH: screen.height,'
                ' innerW: window.innerWidth, innerH: window.innerHeight,'
                ' dpr: window.devicePixelRatio})')
            log(f'화면 정보: {screen_info}')
            results['screen_info'] = json.loads(screen_info)
        except Exception as e:
            log(f'[WARN] 화면 정보 조회 실패: {e}')

        ok_count = 0
        for i, (product_url, sku_name) in enumerate(products, 1):
            log('\n' + '-' * 70)
            log(f'[{i}/{len(products)}] {sku_name}')
            entry = {'product_url': product_url, 'retailer_sku_name': sku_name}
            try:
                crawler.page.get(product_url)
                time.sleep(3)

                asin = crawler.extract_item(product_url)
                entry['asin'] = asin
                tag = f'{i}_{asin or "unknown"}'

                tree = lhtml.fromstring(crawler.page.html)
                review_link_xpath = crawler.xpaths.get('review_link', {}).get('xpath')
                has_review_link = bool(review_link_xpath and tree.xpath(review_link_xpath))
                entry['has_review_link'] = has_review_link

                # 운영과 동일한 리뷰 섹션 이동 (클릭 + 1초 대기 포함)
                crawler.move_to_review_section(has_review_link)

                # 1차: 운영 크롤러와 동일한 타이밍
                diag1, _, _ = diagnose(crawler, 'crawler_timing(+1s)')
                log(f"  [운영 타이밍] matched: {diag1['matched_field']}, "
                    f"컨테이너 {diag1['container_count']}개, 리뷰 {diag1['extracted_review_count']}건")

                # 스크린샷 (운영 타이밍 시점 화면)
                try:
                    crawler.page.get_screenshot(path=out_dir, name=f'{tag}.jpg')
                except Exception as e:
                    log(f'  [WARN] 스크린샷 실패: {e}')

                # 2차: +5초 추가 대기 후 (lazy-load 타이밍 가설 검증)
                time.sleep(5)
                diag2, tree2, page_html2 = diagnose(crawler, 'extra_wait(+5s)')
                log(f"  [+5초 대기] matched: {diag2['matched_field']}, "
                    f"컨테이너 {diag2['container_count']}개, 리뷰 {diag2['extracted_review_count']}건")

                # 리뷰 feature div outerHTML 저장 (신규 레이아웃 분석용)
                sections = tree2.xpath("//div[@id='customer-reviews_feature_div']")
                if sections:
                    snippet = lhtml.tostring(sections[0], encoding='unicode')
                    with open(os.path.join(out_dir, f'{tag}_review_section.html'),
                              'w', encoding='utf-8') as f:
                        f.write(snippet)

                entry['diagnosis'] = [diag1, diag2]
                if diag1['extracted_review_count'] > 0:
                    ok_count += 1
                    entry['verdict'] = 'OK'
                elif diag2['extracted_review_count'] > 0:
                    entry['verdict'] = 'TIMING (추가 대기 후에만 성공)'
                else:
                    hooks = diag2['hook_inventory'] or {}
                    if 'reviewContainer' in hooks or 'review' in hooks:
                        entry['verdict'] = 'XPATH (리뷰는 있는데 xpath 불일치)'
                    else:
                        entry['verdict'] = 'NEW_LAYOUT (섹션에 개별 리뷰 자체가 없음)'
                log(f"  → 판정: {entry['verdict']}")

            except Exception as e:
                log(f'  [ERROR] {e}')
                entry['error'] = str(e)
                entry['verdict'] = 'ERROR'
                traceback.print_exc()

            results['products'].append(entry)
            time.sleep(2)

        log('\n' + '=' * 70)
        verdicts = {}
        for p in results['products']:
            v = p.get('verdict', 'ERROR')
            verdicts[v] = verdicts.get(v, 0) + 1
        log(f'최종 요약: {len(products)}개 중 운영 타이밍 성공 {ok_count}개')
        for v, c in sorted(verdicts.items()):
            log(f'  {v}: {c}개')
        results['summary'] = {'ok_at_crawler_timing': ok_count, 'verdicts': verdicts}
        log(f'결과 폴더: {out_dir}')
        log('=' * 70)
        return True

    finally:
        with open(os.path.join(out_dir, 'summary.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        with open(os.path.join(out_dir, 'results.json'), 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        if crawler.page:
            try:
                crawler.page.quit()
            except Exception:
                pass
        if crawler.db_conn:
            try:
                crawler.db_conn.close()
            except Exception:
                pass


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
