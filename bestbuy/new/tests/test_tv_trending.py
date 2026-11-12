"""TV-only trending recovery, resume, and rank propagation regressions."""

import copy
import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

os.environ.setdefault("BESTBUY_URL_SOURCE", "default")

from bestbuy import bestbuy_orchestrator as orchestrator
from bestbuy import step00_config as config
from bestbuy import step06_trending_deals as trend
from bestbuy import step07_final_targets as final
from bestbuy import step08_detail_enrichment as detail
from bestbuy import step14_db_load as db


TV_URL = "https://www.bestbuy.com/discover/trending-deals/trending-deals-all-tv-home-theater/pcmcat1690836748285"
DISCOVERY_URL = "https://www.bestbuy.com/discover/trending-deals/trending-deals-tvs-projectors/pcmcat1752523988655"
DISCOVERED_URL = "https://www.bestbuy.com/discover/trending-deals/current-television-deals/new-placement"
SECTION = "Trending Deals in TV & Home Theater"


def graph(skus=("1234567", "2345678"), section=SECTION):
    return {"data": {"spotlight": {
        "__typename": "SpotlightProductConnection",
        "storyHeader": section,
        "items": [{
            "__typename": "SpotlightProduct",
            "sku": sku,
            "product": {
                "name": {"short": f"TV {sku}"},
                "url": {"pdp": f"/product/tv/sku/{sku}"},
            },
            "bsin": f"MODEL{sku}",
            "originalSkuId": sku,
        } for sku in skus],
    }}}


def page_html(skus=("1234567", "2345678"), section=SECTION):
    return "<html><script>" + json.dumps(graph(skus, section), separators=(",", ":")) + "</script></html>"


def categories_html(url=DISCOVERED_URL):
    return f"""<html><header><a href="{TV_URL}">TV &amp; Home Theater</a></header>
    <main><section><h2>More Trending Deals categories for you</h2>
    <div role="region" aria-label="carousel">
    <a href="/discover/trending-deals/appliances/other">Major Appliances</a>
    <a href="{url}">TV &amp; Home Theater</a>
    <a href="/discover/trending-deals/music-movies-tv-shows/other">Music, Movies &amp; TV Shows</a>
    </div></section></main></html>"""


class BrowserPage:
    def __init__(self, pages, visits):
        self.pages = pages
        self.visits = visits
        self.html = ""

    def get(self, url):
        self.url = url
        self.visits.append(urlsplit(url).path)
        self.html = self.pages[urlsplit(url).path]


class TvTrendingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.trend_root = self.root / "trending"
        self.csv_path = self.trend_root / "parsed" / "trending_products.csv"
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {
            "BESTBUY_CATEGORY": "TV", "BESTBUY_RUN_ROOT": str(self.root),
            "BESTBUY_URL_SOURCE": "default", "BESTBUY_TRENDING_URL": "",
        }))
        self.stack.enter_context(patch.multiple(
            trend, CATEGORY="TV", RUN_ROOT=self.trend_root, OUTPUT_CSV=self.csv_path,
            TRENDING_URL=TV_URL, TV_DISCOVERY_URL=DISCOVERY_URL,
            BROWSER_WAIT_SECONDS=0, ALLOW_NETWORK_SKU_FALLBACK=False,
            FETCH_MODE="browser_graphql", REQUIRE_ROWS=True,
        ))
        self.stack.enter_context(patch.object(trend, "existing_source_payload_path", return_value=None))

    def browser(self, pages):
        visits = []
        create = self.stack.enter_context(patch.object(
            trend, "create_browser_page",
            side_effect=lambda **kwargs: (BrowserPage(pages, visits), {}),
        ))
        self.stack.enter_context(patch.object(
            trend, "browser_outer_html", side_effect=lambda page, **kwargs: page.html,
        ))
        close = self.stack.enter_context(patch.object(trend, "close_browser_page"))
        return visits, create, close

    def read_rows(self, path=None):
        with (path or self.csv_path).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_defaults_and_csv_use_verified_tv_url_and_preserve_hhp_source(self):
        self.assertEqual(config.BESTBUY_URLS["trending_tvs_projectors"], TV_URL)
        with patch.dict(os.environ, {"BESTBUY_URL_SOURCE": "csv"}):
            self.assertEqual(config.target_url("trend", category="TV"), TV_URL)
            self.assertIn("trending-deals-cell-phones-accessories", config.target_url("trend", category="HHP"))
        self.assertEqual(config.BESTBUY_URLS["main_search"],
                         "https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=tv")
        self.assertIn("sp=Best-Selling", config.BESTBUY_URLS["bsr_search"])
        self.assertEqual(config.BESTBUY_URLS["promotion_tv_home_theater"], config.PROMOTION_TV_HOME_THEATER_URL)

    def test_category_link_uses_section_label_and_dynamic_destination(self):
        self.assertEqual(trend.find_tv_trending_link(categories_html(), DISCOVERY_URL), DISCOVERED_URL)
        aria_heading = categories_html().replace(
            "<h2>", '<div role="heading" aria-level="2"><span>'
        ).replace("</h2>", "</span></div>")
        self.assertEqual(trend.find_tv_trending_link(aria_heading, DISCOVERY_URL), DISCOVERED_URL)
        relative = "/discover/trending-deals/a-new-tv-placement/changed-id"
        self.assertEqual(trend.find_tv_trending_link(categories_html(relative), DISCOVERY_URL),
                         "https://www.bestbuy.com" + relative)

    def test_category_link_rejects_header_only_wrong_domain_and_ambiguity(self):
        for html in (
            f'<header><a href="{TV_URL}">TV &amp; Home Theater</a></header>',
            categories_html("https://example.com/discover/trending-deals/tv"),
            categories_html().replace("</section>", f'<a href="{TV_URL}">Televisions</a></section>'),
            '<main><section><h2>More Trending Deals categories for you</h2></section>'
            f'<section><a href="{TV_URL}">TV &amp; Home Theater</a></section></main>',
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                trend.find_tv_trending_link(html, DISCOVERY_URL)

    def test_direct_collection_publishes_valid_csv_and_success_summary(self):
        visits, create, close = self.browser({urlsplit(TV_URL).path: page_html()})
        self.assertEqual(trend.run(), 0)
        self.assertEqual(visits, [urlsplit(TV_URL).path])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(close.call_count, 1)
        rows = self.read_rows()
        trend.validate_tv_trending_rows(rows)
        self.assertEqual([row["sku_id"] for row in rows], ["1234567", "2345678"])
        self.assertEqual([row["trend_rank"] for row in rows], ["1", "2"])
        summary = json.loads((self.trend_root / "summary_browser_graphql.json").read_text())
        self.assertTrue(summary["success"])
        self.assertEqual(summary["collection_route"], "direct")

    def test_missing_primary_data_recovers_via_category_link(self):
        visits, _, close = self.browser({
            urlsplit(TV_URL).path: "<h1>Empty trending page</h1>",
            urlsplit(DISCOVERY_URL).path: categories_html(),
            urlsplit(DISCOVERED_URL).path: page_html(),
        })
        self.assertEqual(trend.run(), 0)
        self.assertEqual(visits, [urlsplit(url).path for url in [TV_URL, DISCOVERY_URL, DISCOVERED_URL]])
        self.assertEqual(close.call_count, 1)
        summary = json.loads((self.trend_root / "summary_browser_graphql.json").read_text())
        self.assertEqual(summary["collection_route"], "category_link")
        self.assertEqual(summary["url"], DISCOVERED_URL)
        self.assertEqual(summary["attempt_count"], 2)

    def test_invalid_primary_rows_also_trigger_category_recovery(self):
        invalid = {"__trending_rows": trend.parse_trending_products_from_graphql(graph())}
        invalid["__trending_rows"][1]["trend_rank"] = 3
        valid = graph()
        with patch.object(trend, "browser_graphql_once", side_effect=[invalid, valid]) as once, \
                patch.object(trend, "discover_tv_trending_url", return_value=DISCOVERED_URL), \
                patch.object(trend, "create_browser_page", return_value=(Mock(), {})), \
                patch.object(trend, "close_browser_page"):
            rows = trend.response_rows(trend.browser_graphql())
        self.assertEqual(once.call_args_list[1].args, (DISCOVERED_URL,))
        trend.validate_tv_trending_rows(rows)

    def test_stale_original_url_recovers_in_one_browser_without_reloading_discovery(self):
        visits, create, close = self.browser({
            urlsplit(DISCOVERY_URL).path: categories_html(),
            urlsplit(DISCOVERED_URL).path: page_html(),
        })
        with patch.object(trend, "TRENDING_URL", DISCOVERY_URL):
            self.assertEqual(trend.run(), 0)
        self.assertEqual(visits, [urlsplit(DISCOVERY_URL).path, urlsplit(DISCOVERED_URL).path])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(close.call_count, 1)

    def test_zero_rows_fail_clear_stale_csv_and_require_resume(self):
        self.browser({urlsplit(TV_URL).path: "empty", urlsplit(DISCOVERY_URL).path: categories_html(),
                      urlsplit(DISCOVERED_URL).path: "empty"})
        trend.write_rows(self.csv_path, trend.parse_trending_products_from_graphql(graph()))
        self.assertEqual(trend.run(), 1)
        self.assertEqual(self.read_rows(), [])
        summary = json.loads((self.trend_root / "summary_skip.json").read_text())
        self.assertTrue(summary["collection_failed"])
        self.assertFalse(summary["skipped"])
        self.assertEqual([item["route"] for item in summary["attempts"]], ["direct", "category_link"])
        self.assertTrue(all(item["stage"] == "collection" for item in summary["attempts"]))
        self.assertFalse(orchestrator.trending_complete()[0])

    def test_successful_retry_replaces_failure_state_and_completes_resume(self):
        trend.write_failure_skip_summary(ValueError("controlled failure"))
        self.browser({urlsplit(TV_URL).path: page_html()})
        self.assertEqual(trend.run(), 0)
        self.assertTrue(orchestrator.trending_complete()[0])
        summary = json.loads((self.trend_root / "summary_skip.json").read_text())
        self.assertFalse(summary["collection_failed"])

    def test_validator_checks_skus_ranks_and_tv_product_evidence(self):
        valid = trend.parse_trending_products_from_graphql(graph())
        trend.validate_tv_trending_rows(valid)
        for field, value in (("sku_id", "1234567"), ("sku_id", "invalid"),
                             ("trend_rank", "3"), ("trend_section", "Trending Deals in Appliances"),
                             ("trend_section", "Trending Deals in Music, Movies & TV Shows"),
                             ("source", "network_skus")):
            rows = copy.deepcopy(valid)
            rows[1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                trend.validate_tv_trending_rows(rows)
        with self.assertRaises(ValueError):
            trend.validate_tv_trending_rows([])

    def test_existing_parser_deduplicates_repeated_products_in_order(self):
        for rows in [trend.extract_spotlight_product_rows(page_html(("1234567", "1234567", "2345678"))),
                     trend.parse_trending_products_from_graphql(graph(("1234567", "1234567", "2345678")))]:
            trend.validate_tv_trending_rows(rows)
            self.assertEqual([row["sku_id"] for row in rows], ["1234567", "2345678"])

    def test_missing_or_invalid_tv_csv_does_not_count_as_configured_completion(self):
        self.assertFalse(orchestrator.trending_complete()[0])
        rows = trend.parse_trending_products_from_graphql(graph())
        rows[1]["trend_rank"] = ""
        trend.write_rows(self.csv_path, rows)
        self.assertFalse(orchestrator.trending_complete()[0])
        trend.write_rows(self.csv_path, trend.parse_trending_products_from_graphql(graph()))
        self.assertTrue(orchestrator.trending_complete()[0])

    def test_resume_refreshes_join_and_detail_when_trending_is_incomplete(self):
        with patch.object(orchestrator, "step_complete", side_effect=lambda step: (
                step.name != "trending_deals", "controlled completion")):
            names = {step.name for step in orchestrator.resume_steps()}
        self.assertTrue({"trending_deals", "final_targets", "detail_html", "review20"}.issubset(names))
        self.assertTrue({"main_list", "main_targets", "bsr_list", "promotion_deals"}.isdisjoint(names))

    def test_non_tv_collection_and_optional_failure_keep_existing_contract(self):
        with patch.object(trend, "CATEGORY", "HHP"), \
                patch.object(trend, "browser_graphql_once", return_value={"legacy": True}) as once, \
                patch.object(trend, "discover_tv_trending_url") as discovery:
            self.assertEqual(trend.browser_graphql(), {"legacy": True})
            once.assert_called_once_with(TV_URL)
            discovery.assert_not_called()
        with patch.object(trend, "CATEGORY", "HHP"), \
                patch.object(trend, "main", side_effect=ValueError("controlled failure")):
            self.assertEqual(trend.run(), 0)
        with patch.dict(os.environ, {"BESTBUY_CATEGORY": "REF"}), \
                patch.object(orchestrator, "has_target_url", return_value=False):
            self.assertTrue(orchestrator.trending_complete()[0])

    def test_orchestrator_continues_after_tv_optional_enrichment_failure(self):
        import subprocess

        with patch.object(orchestrator.subprocess, "run", side_effect=subprocess.CalledProcessError(1, [])):
            orchestrator.run_step(orchestrator.step_by_key("06"))

    def test_hhp_success_uses_legacy_browser_and_output_path(self):
        section = "Trending Deals in Cell Phones & Accessories"
        visits, _, _ = self.browser({urlsplit(TV_URL).path: page_html(section=section)})
        with patch.object(trend, "CATEGORY", "HHP"):
            self.assertEqual(trend.run(), 0)
        self.assertEqual(len(visits), 1)
        self.assertEqual({row["trend_section"] for row in self.read_rows()}, {section})
        summary = json.loads((self.trend_root / "summary_browser_graphql.json").read_text())
        self.assertTrue(summary["success"])
        self.assertNotIn("collection_route", summary)

    def test_ref_ldy_without_trend_keep_existing_skip_behavior(self):
        for category in ("REF", "LDY"):
            with self.subTest(category=category), patch.object(trend, "CATEGORY", category), \
                    patch.object(trend, "has_target_url", return_value=False), \
                    patch.object(trend, "browser_graphql") as browser:
                self.assertEqual(trend.run(), 0)
                browser.assert_not_called()
                self.assertEqual(self.read_rows(), [])

    def test_actual_csv_join_detail_row_and_insert_values_preserve_sku_rank(self):
        self.browser({urlsplit(TV_URL).path: page_html()})
        self.assertEqual(trend.run(), 0)
        output = self.root / "output"
        target_path = output / "bestbuy_final_targets.csv"
        product_list_path = output / "bestbuy_product_list.csv"
        main_path = self.root / "main.csv"
        final.write_csv(main_path, [{"sku_id": "1234567", "global_visual_rank": "1", "product_name": "Main TV"}])
        with patch.multiple(final, CATEGORY="TV", RUN_ROOT=self.root, MAIN_ROOT=self.root / "main",
                            BSR_ROOT=self.root / "bsr", MAIN_INPUT=main_path,
                            BSR_INPUT=self.root / "bsr.csv", PROMOTION_INPUT=self.root / "promotion.csv",
                            PROMOTION_FALLBACK_INPUT=self.root / "promotion.csv", TRENDING_INPUT=self.csv_path,
                            TRENDING_FALLBACK_INPUT=self.csv_path, OUTPUT_CSV=target_path,
                            PRODUCT_LIST_CSV=product_list_path, FINAL_ROW_LIMIT=0), \
                patch("bestbuy.step00_collection_recovery.assert_ready"), \
                patch("bestbuy.step01_listing_recovery.listing_exhausted", return_value=False):
            final.main()
        targets = final.load_rows(target_path)
        expected = {"1234567": "1", "2345678": "2"}
        self.assertEqual({row["sku_id"]: row["trend_rank"] for row in targets}, expected)
        self.assertEqual({row["sku_id"]: row["trend_rank"] for row in self.read_rows(product_list_path)}, expected)
        with patch.multiple(detail, CATEGORY="TV", RUN_ROOT=self.root, DETAIL_ROOT=self.root / "detail",
                            TARGET_CSV=target_path, OUTPUT_ROOT=output,
                            FINAL_OUTPUT_CSV=output / "final_output.csv",
                            DETAIL_ROWS_CSV=self.root / "detail_rows.csv", USE_DB_SELECTORS=False):
            enriched = [detail.output_row(target) for target in targets]
        final_path = output / "final_output.csv"
        detail.write_csv(final_path, enriched)
        persisted = self.read_rows(final_path)
        self.assertEqual({row["sku_id"]: row["trend_rank"] for row in persisted}, expected)
        for rows in (persisted, self.read_rows(product_list_path)):
            columns = [(name, "integer" if name == "trend_rank" else "text") for name in rows[0]]
            cursor = Mock()
            result = db.insert_rows(cursor, "controlled_test_table", columns, rows)
            inserted = cursor.executemany.call_args.args[1]
            names = result["columns"]
            self.assertEqual({values[names.index("sku_id")]: values[names.index("trend_rank")]
                              for values in inserted}, {key: int(value) for key, value in expected.items()})


if __name__ == "__main__":
    unittest.main()
