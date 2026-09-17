"""Offline replay of PLP inputs and fault cases; no browser or credentials.

Product fields project the 2026-09-17 live SSR samples (1/2/3 labels).
Auxiliary content/rebate responses below are controlled fixtures, not captured
live responses. A live transport acceptance run is still necessary.
"""

import ast
import copy
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "offer_graphql", Path(__file__).resolve().parents[1] / "bestbuy/step00_offer_graphql.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def final_offer(target, products):
    """Evaluate the production output expression without starting detail I/O."""
    path = Path(__file__).resolve().parents[1] / "bestbuy/step08_detail_enrichment.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    counter = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "offer_count")
    output = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "output_row")
    expressions = [value for node in ast.walk(output) if isinstance(node, ast.Dict)
                   for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Constant) and key.value == "offer"]
    assert len(expressions) == 1
    scope = {"target": target, "products": products, "CATEGORY": target.get("category_key", "TV"),
             "graphql_offer_count": api.graphql_offer_count, "uses_graphql_offers": api.uses_graphql_offers,
             "first_non_empty": lambda *args: next((v for v in args if v not in (None, "")), "")}
    exec(compile(ast.Module(body=[counter], type_ignores=[]), str(path), "exec"), scope)
    return eval(compile(ast.Expression(expressions[0]), str(path), "eval"), scope)

CONFIG = {
    "plusX.activatedTopOffers.enabled": False,
    "plusX.caboSuco.enabled": True,
    "plusX.rebates.enabled": True,
    "plusX.rebates.countByContent.enabled": True,
    "plusX.rebates.excludeFromCount.enabled": False,
    "plusX.spendAndGet.enabled": True,
    "plusX.spendAndGetCarousel.enabled": True,
    "plusX.topOffers.countByContent.enabled": True,
    "plusX.topOffers.countByContent.countTotal": False,
    "plusX.topOffers.enabled": True,
    "plusX.topOffers.hotOfferOnly.enabled": True,
    "specialOffersList.offerTypes.allowed": ["PWP Global"],
    "specialOffersList": {"maxOffers": 10, "checkmarkMessagingRequired": True,
                          "filterFinanceMinPurchaseAmount": False, "viewableOfferLimit": 3},
}
PAYLOAD = {"variables": {"productPriceInput": {
    "salesChannel": "LargeView", "customerId": "", "planPaidMemberType": None,
    "usePriceWithCart": True, "context": "plp", "displayLocation": "medium-plp"},
    "destinationZipCode": "10010", "isBestbuyMember": False,
    "skuOffersInput": {"salesChannel": "LargeView", "effectivePlanPaidMemberType": None,
                       "maxOffers": 10, "checkmarkMessagingRequired": True, "filterFinanceMinPurchaseAmount": False}}}


def product(sku="6472693", tier=False, member=False):
    return {"__typename": "Product", "skuId": sku,
            "price": {"__typename": "ItemPrice", "showPlusOffers": True,
                      "openBoxCondition": None, "mobileContracts": [],
                      "giftSkus": [{"skuId": "6597154", "quantity": 1}], "priceWithCart": None,
                      "whatIfPrice": {"planPaidMember2": {"price": 1140.99, "savings": 109},
                                      "planPaidMember3": {"price": 1140.99, "savings": 109}} if member else None,
                      "isEcoRebateEligible": True, "customerSelectedDiscountsAvailable": None,
                      "spendAndGetCabosAvailable": [],
                      "tieredOffersTracking": [{"tieredOffersGroupName": "Tiered Offer - Electrolux"}] if tier else []},
            "offers": {"offers": [{"offerId": "664995", "offerType": "PWP Global",
                                   "hotOffer": True, "complexMemberOffer": False}]}}


def timeline(text="Save with purchase"):
    return {"rows": [{"columns": [{"widgets": [{"content": {"skuPromotionalMessage": text}}]}]}]}


class Replay:
    def __init__(self, products=None, config=None):
        self.products = copy.deepcopy(products if products is not None else [product()])
        self.config = copy.deepcopy(CONFIG if config is None else config)
        self.support = {"o0": {"rows": []}}
        self.support.update({"r" + p["skuId"]: {"skuId": p["skuId"], "ecoRebates": None} for p in self.products})
        self.calls = []
        self.errors = []

    def __call__(self, request):
        self.calls.append(copy.deepcopy(request))
        operation = request["operationName"]
        if operation == "PlatmanQuery":
            return {"data": {"versionedJsonByKey": {"versionedJsonId": "fixture", "json": self.config}}}
        if operation == "OfferCountContent":
            return {"data": self.support, "errors": self.errors}
        raise AssertionError(operation)


def collect(replay, skus=None):
    products = {p["skuId"]: p for p in replay.products}
    rows = [{"category_key": "REF", "sku_id": sku, "offer": "99",
             "raw_product_json": json.dumps(products.get(sku))}
            for sku in (skus or [p["skuId"] for p in replay.products])]
    report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=replay)
    return rows, report


class GraphqlOfferTests(unittest.TestCase):
    def test_captured_rdp_responses_preserve_one_two_three_with_absent_content(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/offer_ref_rdp_20260917.json").read_text(encoding="utf-8"))
        replay = Replay(fixture["products"], fixture["config"])
        replay.support = fixture["support"]["data"]
        replay.errors = fixture["support"]["errors"]
        rows = [{"sku_id": p["skuId"], "category_key": "REF", "raw_product_json": json.dumps(p)} for p in fixture["products"]]
        report = api.collect_graphql_offers(rows, fixture["payload"], None, fetch=replay)
        self.assertTrue(report["complete"])
        self.assertEqual([r["offer"] for r in rows], fixture["expected_counts"])
        self.assertEqual(report["absent_offer_content"]["664995"]["count"], 0)
        self.assertEqual([final_offer(r, []) for r in rows], ["1", "2", "3"])

    def test_not_found_exception_is_limited_to_explicit_content_rows(self):
        for code, path, timeline in (
            ("INTERNAL_SERVER_ERROR", ["o0", "rows"], {"rows": None}),
            ("401", ["o0", "rows"], {"rows": None}),
            ("NOT_FOUND", None, {"rows": None}),
            ("NOT_FOUND", ["o0"], {"rows": None}),
            ("NOT_FOUND", ["o0", "rows"], None),
            ("NOT_FOUND", ["o0", "rows"], {}),
            ("NOT_FOUND", ["o0", "rows", 0, "columns"], {"rows": None}),
        ):
            with self.subTest(code=code, path=path, timeline=timeline):
                replay = Replay()
                replay.support["o0"] = timeline
                replay.errors = [{"message": "failed", "path": path, "extensions": {"code": code}}]
                rows, report = collect(replay)
                self.assertFalse(report["complete"])
                self.assertEqual(rows[0]["offer"], "")

    def test_projected_live_one_two_three_survive_final_detail(self):
        replay = Replay([product(), product("6486389", tier=True), product("6506246", tier=True, member=True)])
        rows, report = collect(replay)
        self.assertTrue(report["complete"])
        self.assertEqual([r["offer"] for r in rows], ["1", "2", "3"])
        self.assertEqual(len(replay.calls), 2)
        self.assertEqual(report["product_source"], "listing_graphql_raw_product_json")
        self.assertEqual([c["operationName"] for c in replay.calls], ["PlatmanQuery", "OfferCountContent"])
        for category in ("REF", "LDY"):
            for row, expected in zip(rows, ("1", "2", "3")):
                target = {**row, "category_key": category}
                self.assertEqual(final_offer(target, [{"price": {"giftSkus": [{}]}}]), expected)
                merged = api.normalize_graphql_offer({"sku_id": row["sku_id"], "category_key": category}, [target])
                self.assertEqual(merged["offer_count"], expected)

    def test_hot_offer_requires_visible_content(self):
        replay = Replay()
        self.assertEqual(collect(replay)[0][0]["offer"], "1")
        replay.support["o0"] = timeline()
        self.assertEqual(collect(replay)[0][0]["offer"], "2")
        replay.support["o0"] = timeline("")
        self.assertEqual(collect(replay)[0][0]["offer"], "1")

    def test_gift_quantity_and_membership_plans_are_not_separate_offers(self):
        p = product(member=True, tier=True)
        p["price"]["giftSkus"][0]["quantity"] = 5
        p["price"]["spendAndGetCabosAvailable"] = [{"offerId": "123"}]
        p["price"]["tieredOffersTracking"] *= 3
        self.assertEqual(collect(Replay([p]))[0][0]["offer"], "3")

    def test_empty_cart_gifts_override_original_gifts(self):
        p = product()
        p["price"]["priceWithCart"] = {"giftSkus": []}
        rows, report = collect(Replay([p]))
        self.assertTrue(report["complete"])
        self.assertEqual(rows[0]["offer"], "")
        self.assertEqual(json.loads(rows[0]["offer_graphql_json"])["components"]["gifts"], 0)

    def test_empty_what_if_object_is_js_truthy(self):
        p = product()
        p["price"]["whatIfPrice"] = {}
        self.assertEqual(collect(Replay([p]))[0][0]["offer"], "2")

    def test_rebate_eligibility_alone_is_not_counted(self):
        replay = Replay()
        program = {"id": "r", "name": "Rebate", "amountLabel": "$10", "formLabel": "Apply",
                   "offerTypes": [], "importantDetails": []}
        replay.support["r6472693"]["ecoRebates"] = {"area": {"zipCode": "10010"},
            "productRebateDetails": [{"rebatePrograms": [program, program]}]}
        self.assertEqual(collect(replay)[0][0]["offer"], "2")
        replay.support["r6472693"]["ecoRebates"]["area"]["zipCode"] = "90001"
        self.assertEqual(collect(replay)[0][0]["offer"], "")

    def test_live_switches_and_content_limits(self):
        replay = Replay()
        replay.config["plusX.topOffers.hotOfferOnly.enabled"] = False
        replay.config["plusX.topOffers.countByContent.countTotal"] = True
        replay.support["o0"] = {"rows": timeline()["rows"] * 5}
        self.assertEqual(collect(replay)[0][0]["offer"], "4")  # 1 gift + capped 3
        replay.config["plusX.giftWithPurchase.enabled"] = False
        replay.config["plusX.topOffers.enabled"] = False
        self.assertEqual(collect(replay)[0][0]["offer"], "")

    def test_non_hot_financing_and_complex_member_are_filtered(self):
        p = product()
        for offer in ({"offerType": "Financing"}, {"hotOffer": False}, {"complexMemberOffer": True}):
            modified = copy.deepcopy(p)
            modified["offers"]["offers"][0].update(offer)
            replay = Replay([modified])
            replay.support["o0"] = timeline()
            self.assertEqual(collect(replay)[0][0]["offer"], "1")

    def test_disabled_label_and_open_box_are_verified_absent(self):
        for key, value in (("showPlusOffers", False), ("openBoxCondition", 0)):
            p = product(member=True, tier=True)
            p["price"][key] = value
            rows, report = collect(Replay([p]))
            self.assertTrue(report["complete"])
            self.assertEqual(rows[0]["offer"], "")

    def test_missing_any_required_field_never_becomes_one(self):
        for key in product()["price"]:
            if key == "__typename":
                continue
            p = product()
            del p["price"][key]
            with self.subTest(key=key):
                rows, report = collect(Replay([p]))
                self.assertFalse(report["complete"])
                self.assertEqual(rows[0]["offer"], "")

    def test_missing_content_or_rebate_is_unknown_not_zero(self):
        for key in ("o0", "r6472693"):
            replay = Replay()
            del replay.support[key]
            rows, report = collect(replay)
            self.assertFalse(report["complete"])
            self.assertEqual(rows[0]["offer_count"], "")

    def test_partial_graphql_error_does_not_invalidate_unrelated_sku(self):
        replay = Replay([product(), product("6486389", tier=True)])
        replay.errors = [{"message": "rebate service unavailable", "path": ["r6472693", "ecoRebates"]}]
        rows, report = collect(replay)
        self.assertEqual([r["offer"] for r in rows], ["", "2"])
        self.assertFalse(report["complete"])
        self.assertEqual(len(replay.calls), 3)  # One bounded support retry.

    def test_failed_graphql_and_config_fail_closed(self):
        rows = [{"sku_id": "6472693", "offer": "1"}]
        calls = []
        def failed(request):
            calls.append(request)
            return {"errors": [{"message": "blocked"}]}
        report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=failed)
        self.assertEqual(len(calls), 2)
        self.assertFalse(report["complete"])
        self.assertEqual(rows[0]["offer"], "")
        for config in ({}, {**CONFIG, "plusX.spendAndGet.enabled": "true"}):
            self.assertFalse(collect(Replay(config=config))[1]["complete"])

    def test_transport_retries_transient_error(self):
        replay = Replay()
        count = 0
        def flaky(request):
            nonlocal count
            count += 1
            if count == 1:
                raise TimeoutError("request timed out")
            return replay(request)
        rows = [{"sku_id": "6472693", "raw_product_json": json.dumps(product())}]
        report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=flaky)
        self.assertTrue(report["complete"])
        self.assertEqual(count, 3)

    def test_real_listing_nullable_fields_are_valid(self):
        p = product("6467055")
        p["price"].update(mobileContracts=None, tieredOffersTracking=None, spendAndGetCabosAvailable=None)
        rows, report = collect(Replay([p]))
        self.assertTrue(report["complete"])
        self.assertEqual(rows[0]["offer"], "1")

    def test_listing_partial_errors_never_turn_null_into_zero(self):
        replay = Replay()
        rows = [{"sku_id": "6472693", "raw_product_json": json.dumps(product())}]
        report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=replay,
                   listing_errors=[{"message": "failed", "path": ["search", "product", "price"]}])
        self.assertFalse(report["complete"])
        self.assertEqual(rows[0]["offer"], "")
        self.assertEqual(replay.calls, [])

    def test_unrelated_listing_errors_do_not_block_valid_offer_inputs(self):
        replay = Replay([product(), product("6486389", tier=True), product("6506246", tier=True, member=True)])
        rows = [{"sku_id": p["skuId"], "raw_product_json": json.dumps(p)} for p in replay.products]
        errors = [{"message": "Error - Internal Server Error", "extensions": {"code": "401"},
                   "path": ["detailedProductSearch", "documents", 0, "product", "fulfillmentOptions"]},
                  {"message": "Error - Not Found", "extensions": {"code": "NOT_FOUND"},
                   "path": ["detailedProductSearch", "documents", 0, "product", "arModels"]},
                  {"message": "Error - Internal Server Error", "path": ["detailedProductSearch", "documents",
                   0, "product", "openBoxOptions", 0, "product", "fulfillmentOptions"]}]
        report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=replay, listing_errors=errors)
        self.assertTrue(report["complete"])
        self.assertEqual([r["offer"] for r in rows], ["1", "2", "3"])
        self.assertEqual(report["ignored_listing_errors"], errors)

    def test_unknown_product_and_offer_error_paths_still_block(self):
        prefix = ["detailedProductSearch", "documents", 0, "product"]
        for path in (None, [], prefix, prefix + ["price"], prefix + ["offers", "offers", 0],
                     prefix + ["price", "fulfillmentOptions"], prefix + ["newUnknownField"], ["detailedProductSearch"]):
            with self.subTest(path=path):
                replay = Replay()
                rows = [{"sku_id": "6472693", "raw_product_json": json.dumps(product())}]
                report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=replay,
                    listing_errors=[{"message": "failed", "path": path}])
                self.assertFalse(report["complete"])
                self.assertEqual(rows[0]["offer"], "")
                self.assertEqual(replay.calls, [])

    def test_sponsored_product_without_price_remains_unknown(self):
        rows = [{"sku_id": "6468484", "raw_product_json": json.dumps({"skuId": "6468484", "condition": "new"})}]
        report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=Replay(), listing_errors=[
            {"path": ["detailedProductSearch", "documents", 0, "product", "arModels"]}])
        self.assertFalse(report["complete"])
        self.assertEqual(rows[0]["offer"], "")
        self.assertIn("missing_price", rows[0]["offer_graphql_json"])

    def test_listing_context_missing_fields_and_wrong_sku_fail_closed(self):
        for raw in (None, "invalid JSON", json.dumps(product("6506246")), json.dumps({"skuId": "6472693"})):
            rows = [{"sku_id": "6472693", "raw_product_json": raw}]
            report = api.collect_graphql_offers(rows, PAYLOAD, None, fetch=Replay())
            self.assertFalse(report["complete"])
            self.assertEqual(rows[0]["offer"], "")
        payload = copy.deepcopy(PAYLOAD)
        payload["variables"]["skuOffersInput"]["maxOffers"] = 1
        rows = [{"sku_id": "6472693", "raw_product_json": json.dumps(product())}]
        report = api.collect_graphql_offers(rows, payload, None, fetch=Replay())
        self.assertFalse(report["complete"])
        self.assertIn("listing_offer_context_mismatch", rows[0]["offer_graphql_json"])

    def test_product_missing_and_wrong_sku_are_never_guessed(self):
        rows, report = collect(Replay(), ["6506246"])
        self.assertFalse(report["complete"])
        self.assertEqual(rows[0]["offer"], "")

    def test_unverified_evidence_does_not_inherit_unmarked_count(self):
        replay = Replay()
        del replay.support["o0"]
        rows, _ = collect(replay)
        old = {"sku_id": "6472693", "offer": "3", "offer_count": "3"}
        self.assertEqual(api.normalize_graphql_offer(rows[0], [old])["offer"], "")

    def test_corrupted_evidence_and_sku_mismatch_are_rejected(self):
        rows, _ = collect(Replay())
        row = rows[0]
        self.assertEqual(api.offer_evidence({**row, "sku_id": "6506246"}), {})
        proof = json.loads(row["offer_graphql_json"])
        proof["count"] = "3"
        self.assertEqual(api.offer_evidence({**row, "offer_graphql_json": json.dumps(proof)}), {})

    def test_personalized_context_is_not_computed_as_guest(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["variables"]["isBestbuyMember"] = True
        replay = Replay()
        rows = [{"sku_id": "6472693"}]
        report = api.collect_graphql_offers(rows, payload, None, fetch=replay)
        self.assertFalse(report["complete"])
        self.assertEqual(rows[0]["offer"], "")
        self.assertEqual(replay.calls, [])

    def test_null_cart_gifts_fall_back_but_malformed_cart_does_not(self):
        p = product()
        p["price"]["priceWithCart"] = {"giftSkus": None}
        self.assertEqual(collect(Replay([p]))[0][0]["offer"], "1")
        p["price"]["priceWithCart"] = {}
        self.assertFalse(collect(Replay([p]))[1]["complete"])

    def test_conflicting_offer_ids_fail_closed(self):
        p = product()
        p["offers"]["offers"].append({**p["offers"]["offers"][0], "complexMemberOffer": True})
        self.assertFalse(collect(Replay([p]))[1]["complete"])

    def test_http_error_and_non_json_do_not_become_zero(self):
        class Browser:
            def __init__(self, result):
                self.result = result
            def run_js(self, script, timeout):
                return self.result
        for response in ({"status": 403, "body": "blocked"}, {"status": 200, "body": "<html>challenge</html>"}):
            rows = [{"sku_id": "6472693", "offer": "1"}]
            report = api.collect_graphql_offers(rows, PAYLOAD, Browser(response))
            self.assertFalse(report["complete"])
            self.assertEqual(rows[0]["offer"], "")
            self.assertEqual(len(report["requests"]), 2)

    def test_fetch_uses_existing_session_only(self):
        class Browser:
            scripts = []
            def run_js(self, script, timeout):
                self.scripts.append(script)
                return json.dumps({"status": 200, "body": '{"data":{}}'})
        browser = Browser()
        self.assertEqual(api.browser_fetch(browser, api.CONFIG_QUERY, 20), {"data": {}})
        self.assertIn("fetch('/gateway/graphql'", browser.scripts[0])
        self.assertNotIn("scroll", browser.scripts[0])
        self.assertNotIn("location", browser.scripts[0])


if __name__ == "__main__":
    unittest.main()
