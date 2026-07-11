"""
Amazon TV dt_update — UC(undetected-chromedriver) 테스트 버전 (측정 전용)

목적:
  리뷰 로그인 게이트가 "세션 신뢰점수 소진"으로 걸린다는 가설 하에,
  UC(SIEL과 동일 브라우저)가 DrissionPage보다 게이트를 늦게/덜 맞는지 실측한다.
  기존 DrissionPage detail의 소진 곡선(2026-07-08: ~130개째부터 게이트)과 비교용.

설계 원칙 (기존 코드 무손상):
  - 기존 dt_update / detail / amazon_base 는 전혀 수정하지 않는다.
  - dt_update 모드 4와 동일한 대상(detailed_review_content IS NULL)을 조회하되,
    기본은 DB 미기록 dry-run — 게이트/성공 여부와 소진 곡선만 측정.
  - 브라우저만 UC로 교체, 나머지(신뢰 프로필, XPath 체인, 게이트 마커)는
    운영 코드에서 그대로 빌려온다.
  - 기본으로 detailed_review_content 를 실제 UPDATE (백필). 측정만 하려면 --dry-run.

사용법 (RDP):
  # 리뷰 백필 (기본 — DB 저장)
  python amazon/tv/amazon_tv_dt_update_uc.py            # batch 선택 프롬프트
  # 측정만 (DB 미기록)
  python amazon/tv/amazon_tv_dt_update_uc.py --dry-run --limit 150

결과: amazon/tv/data/uc_gate_test/<batch>_<ts>.json  (per-product + 소진 곡선)
"""

import sys
import os
import re
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
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException, NoSuchElementException

# 운영 코드에서 상수/헬퍼만 빌려온다 (수정 없음)
from amazon.tv.amazon_tv_dt import (
    TRUSTED_PROFILE_DIR, refresh_trusted_profile,
)
from common.amazon_base import AmazonBaseCrawler
from common.base_crawler import BaseCrawler

REVIEW_GATE_MARKERS = AmazonBaseCrawler.REVIEW_GATE_MARKERS

# UC가 quit()을 GC 시점에 또 시도해 WinError 6 내는 것 차단 (SIEL 패턴)
uc.Chrome.__del__ = lambda self: None


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_review_xpaths(conn):
    """dx_xpath_selectors에서 review 관련 XPath만 로드 (detail용 SEA/TV/Amazon)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT data_field, xpath FROM dx_xpath_selectors
        WHERE corp='SEA' AND product_line='TV' AND account_name='Amazon'
          AND page_type='detail' AND is_active=TRUE AND data_field LIKE 'review%%'
    """)
    xp = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    return xp


def load_targets(conn, batch_id, limit):
    """dt_update 모드 4와 동일 조건: detailed_review_content IS NULL."""
    cur = conn.cursor()
    q = """
        SELECT id, product_url, retailer_sku_name
        FROM tv_retail_com
        WHERE account_name='Amazon' AND batch_id=%s AND product_url IS NOT NULL
          AND detailed_review_content IS NULL
        ORDER BY id
    """
    if limit and limit > 0:
        q += f" LIMIT {int(limit)}"
    cur.execute(q, (batch_id,))
    rows = cur.fetchall()
    cur.close()
    return [{'id': r[0], 'product_url': r[1], 'retailer_sku_name': r[2]} for r in rows]


def detect_chrome_major():
    """설치된 Chrome 메이저 버전 — 레지스트리(BLBeacon)와 디스크상 최신 폴더 중 최대값.

    BLBeacon은 자동 업데이트 후 갱신 지연(stale)될 수 있어, UC version_main이
    실제 실행 바이너리와 어긋나면 즉시 차단된다(과거 MMKT UC 사례). 둘 중 최대 사용.
    """
    versions = []
    try:
        import subprocess
        r = subprocess.run(
            ['reg', 'query', r'HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon', '/v', 'version'],
            capture_output=True, text=True)
        m = re.search(r'(\d+)\.\d+\.\d+\.\d+', r.stdout or '')
        if m:
            versions.append(int(m.group(1)))
    except Exception:
        pass
    for base in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                 os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')):
        appdir = os.path.join(base, 'Google', 'Chrome', 'Application')
        try:
            for name in os.listdir(appdir):
                m = re.match(r'(\d+)\.\d+\.\d+\.\d+$', name)
                if m:
                    versions.append(int(m.group(1)))
        except Exception:
            pass
    return max(versions) if versions else None


def make_uc_driver(user_data_dir=None):
    """SIEL make_driver 설정 이식 + 신뢰 프로필 옵션."""
    opts = uc.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--start-maximized')
    opts.add_argument('--lang=en-US')  # amazon.com US
    opts.add_argument('--disable-background-timer-throttling')
    opts.add_argument('--disable-backgrounding-occluded-windows')
    opts.add_argument('--disable-renderer-backgrounding')
    opts.add_argument('--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling')
    kwargs = {'options': opts}
    if user_data_dir:
        kwargs['user_data_dir'] = user_data_dir
    major = detect_chrome_major()
    if major:
        kwargs['version_main'] = major
        print(f"[INFO] Chrome major={major}")
    driver = uc.Chrome(**kwargs)
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except WebDriverException:
        pass
    try:
        driver.execute_cdp_cmd('Emulation.setFocusEmulationEnabled', {'enabled': True})
    except WebDriverException:
        pass
    return driver


def set_amazon_zip(driver, zip_code='10001'):
    """배송지 ZIP 설정 (리뷰와 무관하지만 detail 조건 동일화용). 실패해도 진행."""
    try:
        driver.get('https://www.amazon.com')
        time.sleep(3)
    except WebDriverException:
        pass


def is_gated(page_source):
    s = (page_source or '').lower()
    return any(m in s for m in REVIEW_GATE_MARKERS)


def scroll_to_reviews(driver):
    try:
        driver.execute_script(
            "var e=document.getElementById('reviewsMedley')"
            "||document.getElementById('customer-reviews_feature_div');"
            "if(e)e.scrollIntoView({block:'center',behavior:'instant'});")
    except WebDriverException:
        pass


def extract_reviews(driver, xpaths, max_reviews=20):
    """review_container 체인 → 매칭 suffix의 review_content로 본문 추출 (운영 로직 이식)."""
    # 컨테이너 체인: base → _fallback1 → _fallback2 (이름 정렬)
    container_fields = ['review_container'] + sorted(
        k for k in xpaths if k.startswith('review_container_fallback'))
    containers, matched = [], None
    for field in container_fields:
        xp = xpaths.get(field)
        if not xp:
            continue
        try:
            els = driver.find_elements(By.XPATH, xp)
        except WebDriverException:
            els = []
        if els:
            containers, matched = els[:max_reviews], field
            break
    if not containers:
        return None, 0

    suffix = matched[len('review_container'):]
    content_field = 'review_content' + suffix
    content_xp = xpaths.get(content_field) or xpaths.get('review_content')
    if not content_xp:
        return None, 0

    reviews = []
    for c in containers:
        try:
            parts = c.find_elements(By.XPATH, content_xp)
        except WebDriverException:
            parts = []
        text = ' '.join((p.text or p.get_attribute('textContent') or '').strip() for p in parts)
        text = ' '.join(text.split())
        if text:
            reviews.append(text)
    if not reviews:
        return None, 0
    formatted = [f"review{i} - {r}" for i, r in enumerate(reviews, 1)]
    return ' ||| '.join(formatted), len(reviews)


def select_batch_id():
    """batch_id 미지정 시 오늘 Amazon TV batch_id 목록 조회 → 번호 선택 (dt_update와 동일 UX)."""
    today_batch_ids = BaseCrawler.fetch_today_batch_ids(
        table_name='tv_retail_com', account_name='Amazon', test_mode=False)
    if today_batch_ids:
        print(f"\n오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id 목록:")
        for i, bid in enumerate(today_batch_ids, 1):
            print(f"  {i}. {bid}")
        print("  0. 직접 입력")
        choice = input("\n번호 선택 ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(today_batch_ids):
            return today_batch_ids[int(choice) - 1]
        if choice == '0':
            return input("batch_id 입력: ").strip()
        if choice and not choice.isdigit():
            return choice
    else:
        print(f"오늘({datetime.now().strftime('%Y-%m-%d')}) Amazon batch_id가 없습니다.")
    return input("batch_id 직접 입력: ").strip()


def main():
    ap = argparse.ArgumentParser(description='Amazon TV dt_update UC — 리뷰(detailed_review_content) 백필/게이트 측정')
    ap.add_argument('--batch-id', help='대상 batch_id (미지정 시 오늘 배치 목록에서 선택)')
    ap.add_argument('--limit', type=int, default=0, help='대상 상품 수 제한 (0=전체)')
    ap.add_argument('--sleep', type=float, default=2.5, help='상품 간 대기(초)')
    ap.add_argument('--no-profile', action='store_true', help='신뢰 프로필 미사용 (기본 사용)')
    ap.add_argument('--dry-run', action='store_true', help='DB 미기록(측정만). 기본은 detailed_review_content 실제 UPDATE')
    args = ap.parse_args()
    write = not args.dry_run

    batch_id = args.batch_id or select_batch_id()
    if not batch_id:
        print("[ERROR] batch_id가 필요합니다.")
        return

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'uc_gate_test')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{batch_id}_{ts}.json')

    conn = db_connect()
    xpaths = load_review_xpaths(conn)
    targets = load_targets(conn, batch_id, args.limit)
    print(f"[INFO] UC 리뷰 백필 — batch={batch_id}, 대상={len(targets)}개, "
          f"write={write}, profile={'off' if args.no_profile else 'on'}")
    print(f"[INFO] review xpaths: {sorted(xpaths)}")
    if not targets:
        print("[INFO] 대상 없음 — 종료")
        conn.close()
        return

    user_data_dir = None
    if not args.no_profile:
        refresh_trusted_profile()
        if os.path.isdir(TRUSTED_PROFILE_DIR):
            user_data_dir = TRUSTED_PROFILE_DIR
            print(f"[INFO] 신뢰 프로필: {user_data_dir}")

    driver = make_uc_driver(user_data_dir=user_data_dir)
    set_amazon_zip(driver)

    results = {'batch_id': batch_id, 'started_at': ts, 'write': write,
               'use_profile': not args.no_profile, 'products': []}
    first_gate_at = None
    ok = gated = 0

    try:
        for i, t in enumerate(targets, 1):
            entry = {'seq': i, 'id': t['id'], 'product_url': t['product_url']}
            try:
                driver.get(t['product_url'])
                time.sleep(3)
                scroll_to_reviews(driver)
                time.sleep(2)
                src = driver.page_source
                g = is_gated(src)
                content, count = (None, 0) if g else extract_reviews(driver, xpaths)
                entry.update({'gated': g, 'review_count': count})
                if g:
                    gated += 1
                    if first_gate_at is None:
                        first_gate_at = i
                elif count > 0:
                    ok += 1
                    if write:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE tv_retail_com SET detailed_review_content=%s, "
                            "crawl_datetime=NOW() WHERE id=%s", (content, t['id']))
                        conn.commit()
                        cur.close()
                        entry['written'] = True
                print(f"[{i}/{len(targets)}] gated={g} reviews={count} "
                      f"(누적 ok={ok} gated={gated})")
            except Exception as e:
                entry['error'] = str(e)[:200]
                print(f"[{i}/{len(targets)}] ERROR: {e}")
            results['products'].append(entry)
            time.sleep(args.sleep)
    finally:
        results['summary'] = {
            'total': len(targets), 'ok': ok, 'gated': gated,
            'first_gate_at': first_gate_at,
        }
        # 25개 버킷 소진 곡선
        buckets = {}
        for p in results['products']:
            b = (p['seq'] - 1) // 25
            buckets.setdefault(b, {'n': 0, 'ok': 0, 'gated': 0})
            buckets[b]['n'] += 1
            if p.get('gated'):
                buckets[b]['gated'] += 1
            elif p.get('review_count', 0) > 0:
                buckets[b]['ok'] += 1
        results['summary']['buckets'] = buckets
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        written = sum(1 for p in results['products'] if p.get('written'))
        print("\n===== UC 리뷰 백필 요약 =====")
        print(f"대상 {len(targets)} | 리뷰추출 {ok} | "
              f"{'DB저장 ' + str(written) if write else 'DRY-RUN(DB 미기록)'} | "
              f"게이트 {gated} | 첫 게이트 순번: {first_gate_at}")
        print("소진 곡선 (25개 버킷): 순번 | ok | gated")
        for b in sorted(buckets):
            v = buckets[b]
            print(f"  {b*25+1:3d}~{b*25+v['n']:3d} | ok={v['ok']:2d} | gated={v['gated']:2d}")
        print(f"결과: {out_path}")
        try:
            driver.quit()
        except Exception:
            pass
        conn.close()


if __name__ == '__main__':
    main()
