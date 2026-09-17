"""Offline checks of the production listing/CSV/final offer path."""

import copy
import csv
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BESTBUY_CATEGORY", "TV")
os.environ.setdefault("BESTBUY_URL_SOURCE", "default")
try:
    import zenrows
except ModuleNotFoundError:
    sys.modules.setdefault("zenrows", types.SimpleNamespace(ZenRowsClient=object))

from bestbuy import step01_main_list as listing
from bestbuy import step02_main_targets as targets
from bestbuy import step04_bsr_rank as ranking
from bestbuy import step07_final_targets as final
from test_offer_graphql import PAYLOAD, Replay, api, collect, final_offer, product


class ApiBrowser:
    """Only implements API transport; unexpected navigation fails the test."""
    url = "https://www.bestbuy.com/"

    def __init__(self, replay):
        self.replay = replay
        self.calls = []

    def run_js(self, script, timeout=None):
        self.calls.append(script)
        raw = script.split("JSON.stringify(", 1)[1]
        request, _ = json.JSONDecoder().raw_decode(raw)
        body = {"data": {"listing": True}} if request["operationName"] == "Listing" else self.replay(request)
        return json.dumps({"status": 200, "contentType": "application/json", "body": json.dumps(body)})


class OfferPipelineTests(unittest.TestCase):
    def listing_page(self, category, replay):
        rows = [{"sku_id": p["skuId"], "category_key": category, "offer_count": "1",
                 "container_type": "organic_product", "global_organic_rank": i,
                 "customer_price": "123.45", "product_name": "sample"}
                for i, p in enumerate(replay.products, 1)]
        browser = ApiBrowser(replay)
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()), patch.multiple(
                listing, CATEGORY=category, RUN_ROOT=Path(folder), BROWSER_GRAPHQL_NAVIGATE_EACH_PAGE=False), \
                patch.object(listing, "parse_page_rows", return_value=rows):
            _, meta, result = listing.browser_graphql_fetch_once(2, {**PAYLOAD, "operationName": "Listing"}, browser)
            reports = list(Path(folder).rglob("*_offers.json"))
            report = json.loads(reports[0].read_text(encoding="utf-8")) if reports else None
        return result, meta, report, browser

    def test_ref_ldy_use_existing_graphql_session_and_carry_proof_to_final_csv(self):
        for category in ("REF", "LDY"):
            with self.subTest(category=category):
                replay = Replay([product(), product("6486389", tier=True), product("6506246", tier=True, member=True)])
                rows, meta, report, browser = self.listing_page(category, replay)
                self.assertEqual(meta["offer_graphql_verified_rows"], 3)
                self.assertEqual(meta["offer_graphql_request_count"], 3)
                self.assertTrue(report["complete"])
                self.assertEqual(len(browser.calls), 4)
                normalized = [targets.normalize_existing_listing_row(row) for row in rows]
                self.assertEqual([row["offer"] for row in normalized], ["1", "2", "3"])
                self.assertEqual([row["customer_price"] for row in normalized], ["123.45"] * 3)
                with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
                    root = Path(folder)
                    with patch.multiple(ranking, RUN_ROOT=root, INPUT_CSV=root / "input.csv", OUTPUT_CSV=root / "bsr.csv"), \
                            patch.object(ranking, "load_rows", return_value=normalized):
                        ranking.main()
                    with (root / "bsr.csv").open(encoding="utf-8-sig", newline="") as stream:
                        bsr_rows = list(csv.DictReader(stream))
                    with patch.object(final, "CATEGORY", category):
                        attrs = final.main_attribute_map(normalized)
                        bsr = final.build_bsr_map(bsr_rows)
                        # Main, BSR-only, and promotion/trending-only target shapes.
                        selected = [normalized[0], final.row_from_bsr_only(bsr_rows[1]), {"sku_id": "6506246"}]
                        enriched = final.enrich_rows(selected, bsr, {}, {}, attrs)
                        final.write_csv(root / "final.csv", enriched)
                    with (root / "final.csv").open(encoding="utf-8-sig", newline="") as stream:
                        persisted = list(csv.DictReader(stream))
                self.assertEqual([final_offer(row, [{"price": {"giftSkus": [{}]}}]) for row in persisted], ["1", "2", "3"])
                self.assertEqual([r["sku_id"] for r in persisted], [r["sku_id"] for r in rows])

    def test_tv_hhp_keep_legacy_offer_without_extra_requests(self):
        for category in ("TV", "HHP"):
            rows, meta, report, browser = self.listing_page(category, Replay())
            self.assertEqual(len(browser.calls), 1)
            self.assertIsNone(report)
            self.assertEqual(meta["offer_graphql_request_count"], 0)
            self.assertNotIn(api.EVIDENCE_FIELD, rows[0])
            self.assertEqual(final_offer({"category_key": category}, [product()]), "1")
            with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
                root = Path(folder)
                with patch.multiple(ranking, RUN_ROOT=root, INPUT_CSV=root / "input.csv", OUTPUT_CSV=root / "bsr.csv"), \
                        patch.object(ranking, "load_rows", return_value=rows):
                    ranking.main()
                with (root / "bsr.csv").open(encoding="utf-8-sig", newline="") as stream:
                    self.assertNotIn(api.EVIDENCE_FIELD, csv.DictReader(stream).fieldnames)

    def test_failed_api_preserves_products_and_reports_unknown(self):
        replay = Replay([product(), product("6486389", tier=True)])
        del replay.support["r6472693"]
        rows, meta, report, _ = self.listing_page("REF", replay)
        self.assertEqual([r["sku_id"] for r in rows], ["6472693", "6486389"])
        self.assertEqual([r["offer"] for r in rows], ["", "2"])
        self.assertFalse(meta["offer_graphql_complete"])
        self.assertEqual(meta["offer_graphql_unverified_rows"], 1)
        self.assertFalse(report["complete"])

    def test_zero_and_failure_cannot_be_replaced_by_legacy_or_other_source_counts(self):
        p = product()
        p["price"]["giftSkus"] = []
        zero = collect(Replay([p]))[0][0]
        unknown = api.normalize_graphql_offer({"sku_id": "6472693", "category_key": "REF", "offer": "9"})
        positive = collect(Replay())[0][0]
        for row in (zero, unknown):
            with patch.object(final, "CATEGORY", "REF"):
                result = final.enrich_rows([row], {"6472693": positive}, {}, {}, {"6472693": positive})[0]
            self.assertEqual(result["offer"], "")
            self.assertEqual(final_offer(result, [product()]), "")
        self.assertEqual(api.offer_evidence(zero)["status"], "verified")
        self.assertEqual(api.offer_evidence(unknown)["status"], "unverified")

    def test_collector_keeps_duplicates_order_and_all_non_offer_fields(self):
        rows = [{"sku_id": sku, "customer_price": "10", "page": "2", "offer": "9"}
                for sku in ("6486389", "6472693", "6486389")]
        before = copy.deepcopy(rows)
        api.collect_graphql_offers(rows, PAYLOAD, None, fetch=Replay([product(), product("6486389", tier=True)]))
        offer_fields = {"offer", "offer_count", api.EVIDENCE_FIELD}
        self.assertEqual([{k: v for k, v in r.items() if k not in offer_fields} for r in rows],
                         [{k: v for k, v in r.items() if k not in offer_fields} for r in before])
        self.assertEqual([r["offer"] for r in rows], ["2", "1", "2"])


if __name__ == "__main__":
    unittest.main()
