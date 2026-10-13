"""Own-product load types, including observed phrases and ownership boundaries."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_dryer import diagnostics, ldy, product, rules, runner, target


class LoadingOwnershipTests(unittest.TestCase):
    def appliance(self, statement="", name="Brand - Electric Dryer"):
        p = product()
        p.update(name={"short": name}, description={"short": None, "long": statement}, features=[])
        return p

    def test_shared_load_modifier_applies_to_both_appliances(self):
        for phrase in (
            "Our front load washers and dryers use tempered glass doors.",
            "Our front-load washer and dryer pair has matching pedestals.",
            "Our front loading washers & dryers have glass doors.",
            "Our front\u2011load washer/dryer pair saves space.",
            "Our top load washers and dryers offer quiet operation.",
        ):
            with self.subTest(phrase=phrase):
                value, evidence = rules.loading_type(self.appliance(phrase))
                self.assertEqual(value, "Topload" if "top load" in phrase else "Frontload")
                self.assertEqual(evidence["rule"], "joint_washer_dryer")
                self.assertEqual(evidence["source_field"], "description.long")

    def test_own_structure_designs_do_not_require_the_word_dryer_in_same_sentence(self):
        cases = (
            ("Stainless steel drum with front loading transparent lid.", "Frontload"),
            ("Our Most Space-Saving Front Load Pair Available", "Frontload"),
            ("This top-loading machine has a stainless steel inner tub.", "Topload"),
            ("Our front loading door makes unloading easy.", "Frontload"),
        )
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(rules.loading_type(self.appliance(phrase))[0], expected)

    def test_single_unit_combo_does_not_lose_its_own_loading_type(self):
        p = self.appliance(name="Brand - Washer/Dryer Combo")
        p["features"] = [{"description": "This mega capacity washer/dryer combo is among the largest front load all-in-one units."}]
        value, evidence = rules.loading_type(p)
        self.assertEqual(value, "Frontload")
        self.assertEqual(evidence["source_field"], "features")
        p["features"] = [{"description": "This top-loading machine has a stainless steel inner tub."}]
        self.assertEqual(rules.loading_type(p)[0], "Topload")

    def test_washer_owned_phrases_never_borrow_a_dryer_elsewhere_in_sentence(self):
        phrases = (
            "This dryer handles loads washed in a top load washer.",
            "This dryer has a front load washer nearby.",
            "Our washer has a top-loading design that works with this dryer.",
            "This washer has a front loading transparent lid.",
            "The front load washer and electric dryer are sold together.",
            "Front Load 4.5 Cu. Ft. Washer and 7.4 Cu. Ft. Electric Dryer Laundry Center",
            "Top Load Washer and 5.9 Cu. Ft. Electric Dryer",
            "Pair with matching front load washers and dryers.",
            "Compatible with our front load washer and dryer pair.",
            "For use with front load washers and dryers.",
            "Front load design for the matching washer.",
            "Pair with a top load machine.",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual(rules.loading_type(self.appliance(phrase))[0], "")

    def test_own_dryer_and_other_washer_clauses_can_share_a_sentence(self):
        for phrase, expected in (
            ("This front-load dryer pairs with a top-load washing machine.", "Frontload"),
            ("This top load dryer handles clothes from a front load washer.", "Topload"),
            ("This dryer has a front loading transparent lid and compatibility with a variety of other washers.", "Frontload"),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(rules.loading_type(self.appliance(phrase))[0], expected)

    def test_generic_machine_structure_requires_own_drying_appliance_context(self):
        p = self.appliance("This top-loading machine has a stainless steel tub.", "Brand - Washing Machine")
        self.assertEqual(rules.loading_type(p)[0], "")
        p["specificationGroups"][0]["specifications"].append({"displayName": "Washer Load Type", "value": "Top load"})
        self.assertEqual(rules.loading_type(p)[0], "")

    def test_name_fallback_keeps_washer_modifiers_separate(self):
        names = (
            "Brand - Front Load Washer and Electric Dryer Combo",
            "Brand - Top Load Washer and 5.9 Cu. Ft. Electric Dryer Laundry Center",
            "Brand - Front Load 4.5 Cu. Ft. Washer & 8 Cu. Ft. Gas Dryer",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(rules.loading_type(self.appliance(name=name))[0], "")
        p = self.appliance(name="Brand - Front Load Electric Dryer with Washer Link")
        self.assertEqual(rules.loading_type(p)[0], "Frontload")

    def test_unqualified_structure_on_a_two_unit_tower_is_not_assigned_to_dryer(self):
        for phrase in ("This top-loading machine handles laundry.", "Our front loading transparent lid is durable."):
            with self.subTest(phrase=phrase):
                p = self.appliance(phrase, "Brand - Washer and Electric Dryer Laundry Center")
                p["specificationGroups"] = [{"specifications": [
                    {"displayName": "Washer Capacity", "value": "3.8 cubic feet"},
                    {"displayName": "Dryer Capacity", "value": "5.9 cubic feet"}]}]
                self.assertEqual(rules.loading_type(p)[0], "")
                p["description"]["long"] = "This dryer has a front loading door."
                self.assertEqual(rules.loading_type(p)[0], "Frontload")
                p["description"]["long"] = "This front load pair saves space."
                self.assertEqual(rules.loading_type(p)[0], "Frontload")

    def test_conflicts_and_explicit_specification_priority_are_preserved(self):
        p = self.appliance("This front-load dryer is efficient.", "Brand - Front Load Dryer")
        p["features"] = [{"description": "This top-loading machine has a stainless steel tub."}]
        self.assertEqual(rules.loading_type(p), ("", {"source": "conflicting_features"}))
        p["specificationGroups"][0]["specifications"].append({"displayName": "Dryer Load Type", "value": "Front Load"})
        self.assertEqual(rules.loading_type(p)[0], "Front Load")
        p["specificationGroups"][0]["specifications"].append({"displayName": "Dryer Loading Type", "value": "Top load"})
        self.assertEqual(rules.loading_type(p), ("", {"source": "conflicting_specifications"}))

    def test_related_feature_identity_is_checked_for_sku_and_bsin(self):
        p = self.appliance()
        p["features"] = [
            {"skuId": "other", "description": "Our front load washers and dryers have glass doors."},
            {"bsin": "different", "description": "This top-loading machine dries clothes."},
        ]
        self.assertEqual(rules.loading_type(p)[0], "")
        p["featureGroups"] = [{"bsin": p["bsin"], "features": [{"description": "Our front load washers and dryers have glass doors."}]}]
        self.assertEqual(rules.loading_type(p)[0], "Frontload")
        self.assertEqual(rules.loading_type(p)[1]["source_field"], "featureGroups")

    def test_source_evidence_is_kept_without_changing_product_data(self):
        p = self.appliance()
        p["features"] = [{"description": "Our front load washers and dryers use tempered glass doors."}]
        before = copy.deepcopy(p)
        value, evidence = rules.loading_type(p)
        self.assertEqual(value, "Frontload")
        self.assertEqual(evidence["source_field"], "features")
        self.assertIn("washers and dryers", evidence["evidence"])
        self.assertEqual(p, before)


class LoadingEvidenceFlowTests(unittest.TestCase):
    def test_reused_detail_logs_loading_evidence_without_an_api_call(self):
        p = product()
        p["description"] = {"short": None, "long": None}
        p["features"] = [{"description": "Our front load washers and dryers have glass doors."}]
        capture = {"collector_version": runner.COLLECTOR_VERSION, "product": p,
                   "captured_at": "2026-10-12T01:09:56", "transport": "browser_graphql"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()), diagnostics.run_logging(root), patch.object(runner, "collect_product_batch") as request:
                rows, evidence, failures = runner.collect_details((None, None, ldy), root, [target()], "b_test", 5,
                                                                {"captures": {p["skuId"]: capture}})
            events = [json.loads(line) for line in (root / "logs/dryer_events.jsonl").read_text(encoding="utf-8").splitlines()]
        request.assert_not_called()
        self.assertFalse(failures)
        self.assertEqual(len(rows[0]), 20)
        self.assertEqual(rows[0]["loading_type"], "Frontload")
        collected = next(e for e in events if e["event"] == "attribute_collected")
        self.assertEqual((collected["field"], collected["value"], collected["source_field"]), ("loading_type", "Frontload", "features"))
        self.assertEqual(collected["evidence"], evidence[0]["evidence"])
        self.assertEqual(collected["rule"], "joint_washer_dryer")


if __name__ == "__main__":
    unittest.main()
