"""Offline DRYER/LDY checks; fixtures contain only manually observed public data."""
import copy
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Disable actual secret loading/DB URL lookups during offline tests.
os.environ["BESTBUY_ENV_PATH"] = str(Path(tempfile.gettempdir()) / "bby_dryer_offline_no_env")
os.environ["BESTBUY_URL_SOURCE"] = "default"
os.environ["BESTBUY_CATEGORY"] = "LDY"

from bestbuy import step08_detail_enrichment as ldy
from bestbuy import step01_main_list as listing
from bestbuy import step00_dryer as rules
from bestbuy import step17_dryer as runner
from bestbuy import step00_dryer_log as diagnostics


def product(sku_id="6471411", model="ELFE7637AT", capacity="8 cubic feet"):
    return {"skuId":sku_id,"bsin":"J7CJ36K4TC", "name":{"short":"Electrolux - 8.0 Cu. Ft. Stackable Electric Dryer with Steam"},
            "manufacturer":{"modelNumber":model}, "url":{"pdp":"/product/electrolux-dryer/J7CJ36K4TC/sku/"+sku_id},
            "description":{"short":"This front load electric dryer keeps clothes looking their best."},
            "reviewInfo":{"averageRating":4.2,"reviewCount":152},
            "specificationGroups":[{"specifications":[{"displayName":"Capacity","value":capacity},
                {"displayName":"Matching Washer Type","value":"Topload"}]}],
            "features":[], "price":{"customerPrice":999.99,"regularPrice":1214.99,"totalSavings":215}}


def target(sku_id="6471411"):
    return {"sku_id":sku_id,"product_name":"Electric Dryer","main_rank":3,"bsr_rank":1}


class ExtractionTests(unittest.TestCase):
    def test_three_public_capacity_shapes_and_ldy_unchanged(self):
        for value in ("7.3 cubic feet", "7 cubic feet", "8 cubic feet"):
            p = product(capacity=value)
            p["specificationGroups"][0]["specifications"].append({"displayName":"Washer Load Type","value":"Topload"})
            self.assertEqual(ldy.ldy_attributes_from_product([p], p["name"]["short"]), {"ldy_capacity":value,"ldy_loading_type":"Topload"})
            row, _ = rules.make_row(target(), p, ldy, "b_test", datetime(2026,10,8))
            self.assertEqual(row["capacity"], value)
            self.assertEqual(row["loading_type"], "Frontload")

    def test_dryer_capacity_label_variants_and_washer_capacity_exclusion(self):
        p=product()
        for label in ("Dryer Capacity", "Dryer Capacity (cu. ft.)"):
            p["specificationGroups"]=[{"specifications":[{"displayName":label,"value":"7.3 cubic feet"}]}]
            self.assertEqual(rules.dryer_capacity(p,ldy),"7.3 cubic feet")
        p["specificationGroups"]=[{"specifications":[{"displayName":"Washer Capacity","value":"4.5 cubic feet"}]}]
        self.assertEqual(rules.dryer_capacity(p,ldy),"")

    def test_matching_washer_is_never_dryer_loading(self):
        p = product()
        p["description"]["short"] = "This dryer matches a front-load washer."
        self.assertEqual(rules.loading_type(p)[0], "")
        self.assertEqual(rules.loading_type(p,["Matching washer has a top-load design."])[0], "")

    def test_features_and_explicit_specs_have_defined_priority(self):
        p = product()
        p["description"] = {}
        self.assertEqual(rules.loading_type(p,["Modern front-load design for drying clothes."])[0], "Frontload")
        p["specificationGroups"][0]["specifications"].append({"displayName":"Dryer Load Type","value":"Front Load"})
        self.assertEqual(rules.loading_type(p,["top-load design"])[0], "Front Load")

    def test_no_loading_evidence_stays_blank(self):
        p = product()
        p["description"] = {"short":"A powerful sensor dryer."}
        self.assertEqual(rules.loading_type(p), ("",{"source":"not_stated"}))

    def test_conflicting_loading_is_reported(self):
        p = product()
        p["description"] = {}
        self.assertEqual(rules.loading_type(p,["front-load design", "top-load design"])[1]["source"], "conflicting_features")

    def test_contract_and_identifiers_and_primary_offer(self):
        p = product()
        p["buyingOptions"] = [{"product":{"price":{"customerPrice":849.99}}}]
        row, _ = rules.make_row(target(),p,ldy,"b_test",datetime(2026,10,8))
        self.assertEqual(tuple(row), rules.FIELDS)
        self.assertEqual(len(row),20)
        self.assertEqual((row["item"],row["sku"]),("J7CJ36K4TC","ELFE7637AT"))
        self.assertEqual((row["final_sku_price"],row["original_sku_price"],row["savings"]),("$999.99","$1,214.99","$215"))
        self.assertNotIn("buyingOptions", rules.public_product(p))
        self.assertEqual(row["count_of_reviews"],row["count_of_star_ratings"])

    def test_no_price_does_not_use_other_offer(self):
        p = product()
        p["price"] = {}
        p["buyingOptions"] = [{"product":{"price":{"customerPrice":849.99}}}]
        with self.assertRaisesRegex(ValueError,"missing_required"):
            rules.make_row(target(),p,ldy,"b_test",datetime.now())

    def test_bare_dryer_name_needs_laundry_appliance_evidence(self):
        p=product()
        p["name"]={"short":"Brand - Professional Blow Dryer"}
        p["specificationGroups"]=[]
        with self.assertRaisesRegex(ValueError,"laundry_dryer_type_not_verified"):
            rules.make_row(target(),p,ldy,"b_test",datetime.now())

    def test_verified_dryer_can_have_unstated_capacity(self):
        p=product()
        p["name"]={"short":"Brand - Electric Dryer"}
        p["specificationGroups"]=[{"specifications":[{"displayName":"Dryer Heating Source","value":"Electric"}]}]
        row,evidence=rules.make_row(target(),p,ldy,"b_test",datetime.now())
        self.assertEqual(row["capacity"],"")
        self.assertEqual(evidence["capacity_source"],"not_stated")

    def test_wrong_sku_and_url_fail(self):
        with self.assertRaisesRegex(ValueError,"identity_mismatch"):
            rules.make_row(target("6529909"),product(),ldy,"b_test",datetime.now())
        self.assertEqual(rules.primary_url("https://www.bestbuy.com/product/x/Y/sku/1", "2"), "")
        self.assertEqual(rules.primary_url("https://example.com/product/x", "2"), "")

    def test_zero_reviews_and_unknown_reviews_differ(self):
        p = product()
        p["reviewInfo"] = {"averageRating":0,"reviewCount":0}
        row,_ = rules.make_row(target(),p,ldy,"b_test",datetime.now())
        self.assertEqual((row["count_of_reviews"],row["star_rating"]),("0","Not yet reviewed"))
        p["reviewInfo"] = {"averageRating":"0","reviewCount":"0"}
        row,_ = rules.make_row(target(),p,ldy,"b_test",datetime.now())
        self.assertEqual((row["count_of_reviews"],row["star_rating"]),("0","Not yet reviewed"))
        p["reviewInfo"] = {}
        row,_ = rules.make_row(target(),p,ldy,"b_test",datetime.now())
        self.assertEqual((row["count_of_reviews"],row["star_rating"]),("",""))


class ListingTests(unittest.TestCase):
    def test_standalone_selection(self):
        for title in ("7.3 Cu. Ft. Electric Dryer", "8 Cu. Ft. Stackable Gas Dryer", "Portable Clothes Dryer", "Heat Pump Dryer", "Electric Dryer with Drying Rack"):
            self.assertTrue(rules.is_standalone_dryer(title),title)
        for title in ("Hair Dryer", "Hair-Dryer", "Hand Dryer", "Washer and Dryer Combo", "Electric Dryer Cord", "Dryer Vent Kit", "Washer", "Laundry Center with Dryer", "Dryer Rack"):
            self.assertFalse(rules.is_standalone_dryer(title),title)

    def test_original_keyword_ranks_survive_filtering_and_duplicates(self):
        main = [{"sku_id":"1","product_name":"Washer","container_type":"organic_product"},
                {"sku_id":"2","product_name":"Electric Dryer","container_type":"sponsored_ingrid"},
                {"sku_id":"2","product_name":"Electric Dryer","container_type":"organic_product"},
                {"sku_id":"3","product_name":"Gas Dryer","container_type":"organic_product"}]
        bsr = [dict(main[1]),dict(main[3]),dict(main[0]),dict(main[2])]
        rows = rules.merge_targets(main,bsr)
        self.assertEqual([(r["sku_id"],r["main_rank"],r["bsr_rank"]) for r in rows],[("2",2,3),("3",3,1)])

    def test_reuses_existing_api_parser(self):
        rows = listing.parse_page_rows(1, api_graph([6471411]))
        self.assertEqual(rows[0]["sku_id"],"6471411")
        self.assertEqual(rows[0]["container_type"],"organic_product")





class FakeCursor:
    def __init__(self, generated=True):
        self.calls=[]
        self.generated=generated
        self.inserted=[]
    def execute(self,sql,values=None):
        self.calls.append((sql,values))
    def executemany(self,sql,values):
        self.calls.append((sql,values))
        self.inserted=values
    def fetchall(self):
        return [(f,"integer" if f in {"id","main_rank","bsr_rank"} else "text", f=="id" and self.generated,"NO") for f in rules.FIELDS]
    def fetchone(self):
        return (len(self.inserted),)
    def __enter__(self): return self
    def __exit__(self,*args): return False


class FakeConnection:
    def __init__(self,cursor): self.c=cursor; self.closed=False
    def cursor(self): return self.c
    def close(self): self.closed=True
    def __enter__(self): return self
    def __exit__(self,*args): return False


class DatabaseTests(unittest.TestCase):
    def row(self):
        return rules.make_row(target(),product(),ldy,"b_test",datetime.now())[0]
    def test_load_is_confined_to_test_table_and_same_batch(self):
        cursor=FakeCursor()
        connection=FakeConnection(cursor)
        with patch.object(runner,"connect_db",return_value=connection),patch.dict(os.environ,{"BESTBUY_OUTPUT_TABLE":"tv_retail_com"}):
            self.assertEqual(runner.load_test_table(None,[self.row()],"b_test"),1)
        sql="\n".join(call[0] for call in cursor.calls)
        self.assertNotIn("tv_retail_com",sql)
        self.assertIn("DELETE FROM public.ldy_dryer_retail_test WHERE batch_id=%s AND account_name=%s",sql)
        self.assertTrue(connection.closed)
        self.assertEqual(len(cursor.inserted[0]),19)
    def test_common_insert_formats_match_existing_bby(self):
        from bestbuy.step14_db_load import normalize_value
        for rating, expected in ((4.0, "4"), ("5.0", "5"), (4.5, "4.5"),
                                 ("Not yet reviewed", "Not yet reviewed")):
            row = self.row()
            row.update(star_rating=rating, bsr_rank="", count_of_reviews="1,234",
                       count_of_star_ratings="1,234", main_rank="3")
            cursor = FakeCursor()
            with self.subTest(rating=rating), patch.object(runner, "connect_db", return_value=FakeConnection(cursor)):
                self.assertEqual(runner.load_test_table(None, [row], "b_test"), 1)
                values = dict(zip(rules.FIELDS[1:], cursor.inserted[0]))
                self.assertEqual(values["star_rating"], expected)
                self.assertEqual(values["main_rank"], 3)
                self.assertIsNone(values["bsr_rank"])
                self.assertEqual(values["count_of_reviews"], "1,234")
                types = {name: dtype for name, dtype, _, _ in cursor.fetchall()}
                self.assertEqual(values, {field: normalize_value(row[field], types[field], field)
                                          for field in rules.FIELDS[1:]})

    def test_new_table_reuses_ldy_types_and_only_requested_columns(self):
        class MissingTableCursor(FakeCursor):
            def __init__(self):
                super().__init__()
                self.schema_reads = 0
            def fetchall(self):
                self.schema_reads += 1
                return [] if self.schema_reads == 1 else super().fetchall()
        cursor = MissingTableCursor()
        with patch.object(runner, "connect_db", return_value=FakeConnection(cursor)):
            self.assertEqual(runner.load_test_table(None, [self.row()], "b_test"), 1)
        statements = [sql for sql, _ in cursor.calls if sql.startswith("CREATE TABLE")]
        self.assertEqual(len(statements), 1)
        ddl = statements[0]
        self.assertIn("CREATE TABLE public.ldy_dryer_retail_test", ddl)
        self.assertIn('"country" varchar(10) NOT NULL DEFAULT', ddl)
        self.assertIn('"account_name" varchar(50) NOT NULL DEFAULT', ddl)
        self.assertIn('"calendar_week" varchar(20)', ddl)
        self.assertIn('"crawl_datetime" varchar(50)', ddl)
        self.assertIn('"loading_type" text', ddl)
        self.assertIn('"capacity" text', ddl)
        self.assertEqual(ddl.count('"'), 40)
        self.assertNotIn('"product"', ddl)
        self.assertNotIn('"page_type"', ddl)
        self.assertNotIn('"ldy_loading_type"', ddl)
        self.assertNotIn('"crawl_strdatetime"', ddl)

    def test_schema_failure_occurs_before_delete(self):
        cursor=FakeCursor(generated=False)
        with patch.object(runner,"connect_db",return_value=FakeConnection(cursor)):
            with self.assertRaisesRegex(runner.DryerError,"id_must_be_generated"):
                runner.load_test_table(None,[self.row()],"b_test")
        self.assertFalse(any("DELETE" in call[0] for call in cursor.calls))
    def test_wrong_batch_or_duplicate_prevents_any_db_connection(self):
        row=self.row()
        with patch.object(runner,"connect_db") as connect:
            with self.assertRaises(runner.DryerError): runner.load_test_table(None,[row],"different")
            with self.assertRaises(runner.DryerError): runner.load_test_table(None,[row,row],"b_test")
            connect.assert_not_called()
    def test_retry_uses_successful_capture_and_skips_load_on_failure(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)
            cache=run/"products"/"6471411.json"
            runner.write_json(cache,{"collector_version":2,"product":product(),"captured_at":"2026-10-08T10:00:00"})
            runner.write_json(run/"dryer_manifest.json",{"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            config=SimpleNamespace(bestbuy_zip_code=lambda:"10010",bestbuy_store_id=lambda:"482")
            runtime=(config,listing,ldy)
            rows=[dict(target(),product_name="Electric Dryer",container_type="organic_product"),dict(target("2"),product_name="Gas Dryer",container_type="organic_product")]
            with patch.object(runner,"load_runtime",return_value=runtime),patch.object(runner,"connect_db",return_value=FakeConnection(FakeCursor())),patch.object(runner,"collect_listing",side_effect=[rows,[]]),patch.object(runner,"collect_product_batch",side_effect=runner.DryerError("http_401")) as capture,patch.object(runner,"load_test_table") as load,redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume",str(run)]),1)
            self.assertEqual(capture.call_count,1)
            load.assert_not_called()
            manifest=json.loads((run/"dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["batch_id"],"b_existing")
            self.assertEqual(manifest["collected_count"],1)
            self.assertFalse(manifest["db_loaded"])

    def test_complete_run_loads_exact_contract_and_preserves_capture_time(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)
            runner.write_json(run/"dryer_manifest.json",{"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            config=SimpleNamespace(bestbuy_zip_code=lambda:"10010",bestbuy_store_id=lambda:"482")
            runtime=(config,listing,ldy)
            rows=[dict(target(),product_name="Electric Dryer",container_type="organic_product")]
            cursor=FakeCursor()
            captured={"collector_version":2,"product":product(),"captured_at":"2026-10-08T10:00:00"}
            with patch.object(runner,"load_runtime",return_value=runtime),patch.object(runner,"connect_db",side_effect=[FakeConnection(FakeCursor()),FakeConnection(cursor)]),patch.object(runner,"collect_listing",side_effect=[rows,[]]),patch.object(runner,"collect_product_batch",return_value=({"6471411":captured},{})),redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume",str(run)]),0)
            manifest=json.loads((run/"dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["inserted_count"],1)
            self.assertTrue(manifest["db_loaded"])
            self.assertEqual(cursor.inserted[0][rules.FIELDS[1:].index("crawl_datetime")],"2026-10-08 10:00:00")

    def test_invalid_resume_does_not_overwrite_original_manifest(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory)
            original={"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing","status":"success","zip_code":"10010","store_id":"482"}
            runner.write_json(run/"dryer_manifest.json",original)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume",str(run),"--main-limit","0","--bsr-limit","0","--no-load"]),1)
            self.assertEqual(json.loads((run/"dryer_manifest.json").read_text(encoding="utf-8")),original)
            config=SimpleNamespace(bestbuy_zip_code=lambda:"90210",bestbuy_store_id=lambda:"482")
            with patch.object(runner,"load_runtime",return_value=(config,listing,ldy)),redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume",str(run),"--no-load"]),1)
            self.assertEqual(json.loads((run/"dryer_manifest.json").read_text(encoding="utf-8")),original)

    def test_missing_resume_does_not_create_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing=Path(directory)/"wrong-directory"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume",str(missing),"--no-load"]),1)
            self.assertFalse(missing.exists())


def api_product(sku_id):
    p = product(str(sku_id))
    p["bsin"] = "ITEM" + str(sku_id)
    p["url"]["pdp"] = "/product/sample/" + p["bsin"] + "/sku/" + str(sku_id)
    return p


def api_graph(skus):
    return {"data": {"detailedProductSearch": {"documents": [
        {"product": api_product(sku)} for sku in skus]}}}


def list_row(sku, rank, bsr=False):
    return {"sku_id": str(sku), "bsin": "ITEM" + str(sku), "product_name": "Electric Dryer",
            "container_type": "organic_product", "global_visual_rank": rank,
            "global_organic_rank": rank, "visual_rank": rank}


class ApiFlowTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.multiple(listing, CATEGORY="DRYER", SEARCH_TERM="DRYER",
            SEARCH_URL_TEMPLATE="", SEARCH_SORT="", LISTING_MAX_ATTEMPTS=2,
            LISTING_PAGE_SLEEP_SECONDS=0)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.config = SimpleNamespace(apply_bestbuy_location=lambda v: v,
            bestbuy_zip_code=lambda: "10010", bestbuy_store_id=lambda: "482")
        self.runtime = (self.config, listing, ldy)
        self.operation = {"operationName": "PlpView_ProductList_Init",
            "query": "query PlpView_ProductList_Init{detailedProductSearch{documents{product{skuId}}}}",
            "variables": {"input": {}, "detailedSearchInput": {}, "sort": {}, "pagination": {}}}

    def test_default_main20_bsr10_and_batch5(self):
        args = runner.parse_args([])
        self.assertEqual((args.main_limit, args.bsr_limit, args.detail_batch_size), (20, 10, 5))

    def test_main20_bsr10_union_preserves_both_ranks(self):
        main = [list_row(i, i) for i in range(1, 26)]
        bsr = [list_row(i, n) for n, i in enumerate(range(16, 26), 1)]
        result = rules.merge_targets(main, bsr, 20, 10)
        self.assertEqual(len(result), 25)
        by_sku = {r["sku_id"]: r for r in result}
        self.assertEqual((by_sku["16"]["main_rank"], by_sku["16"]["bsr_rank"]), (16, 1))
        self.assertEqual(by_sku["21"]["main_rank"], "")
        self.assertEqual(by_sku["1"]["bsr_rank"], "")

    def test_ldy_bsin_and_url_identity_keep_original_sku(self):
        main = list_row(1, 1)
        main["bsin"] = ""
        main["product_url"] = "https://www.bestbuy.com/product/sample/ITEM1/sku/1"
        bsr = dict(list_row(2, 1), bsin="item1")
        result = rules.merge_targets([main], [bsr], 20, 10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sku_id"], "1")
        self.assertEqual(result[0]["bsr_rank"], 1)

    def test_capacity_uses_specs_despite_feature_title_conflict(self):
        p = product(capacity="7.3 cubic feet")
        p["description"] = {"short": "Sensor drying system."}
        p["features"] = [{"title": "7.4 cu. ft. Ultra Large Capacity", "description": "7.3 cu. ft. of space."}]
        row, evidence = rules.make_row(target(), p, ldy, "b_test", datetime.now())
        self.assertEqual(row["capacity"], "7.3 cubic feet")
        self.assertEqual(row["loading_type"], "")
        self.assertEqual(evidence["source"], "not_stated")

    def test_observed_insignia_features_ignore_top_load_washer(self):
        p = product(capacity="7 cubic feet")
        p["description"] = {}
        p["features"] = [
            {"title": "7 cu. ft. capacity with front-load design", "description": "Allows you to unload laundry from the dryer."},
            {"title": "Matching washer", "description": "Pair with the Insignia 4.1 Cu. Ft. Top Load Washer with ColdMotion Technology (NS-WMT41WA5)."}]
        self.assertEqual(rules.loading_type(p)[0], "Frontload")
        p["features"] = [{"skuId": "other", "description": "top-load design"}]
        self.assertEqual(rules.loading_type(p)[0], "")

    def test_listing_collects_pages_until_twenty_dryers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, api_graph(range(1, 9))), (200, api_graph(range(9, 21)))]) as post, redirect_stdout(io.StringIO()):
            rows = runner.collect_listing(self.runtime, Path(directory), "main", 5, 20)
        self.assertEqual(len(rules.merge_targets(rows, [], 20, 0)), 20)
        self.assertEqual(post.call_count, 2)
        variables = post.call_args.args[1]["variables"]
        self.assertEqual(variables["pagination"]["pageNumber"], 2)
        self.assertEqual(variables["input"]["query"], "DRYER")
        self.assertEqual(variables["sort"]["sort"], "")

    def test_bsr_best_selling_reuses_verified_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, api_graph(range(1, 11)))) as post, redirect_stdout(io.StringIO()):
            root = Path(directory)
            first = runner.collect_listing(self.runtime, root, "bsr", 5, 10)
            second = runner.collect_listing(self.runtime, root, "bsr", 5, 10)
        self.assertEqual(first, second)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[1]["variables"]["sort"]["sort"], "Best-Selling")
        self.assertEqual(listing.SEARCH_SORT, "")

    def test_full_listing_requires_verified_empty_api_page(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=[(200, api_graph([1, 2])), (200, api_graph([]))]) as post, redirect_stdout(io.StringIO()):
            rows = runner.collect_listing(self.runtime, Path(directory), "main", 5, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(post.call_count, 2)
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, {"data": {}})), patch.object(listing, "listing_retry_delay", return_value=0):
            with self.assertRaises(runner.DryerError):
                runner.collect_listing(self.runtime, Path(directory), "main", 2, 0)

    def test_repeated_page_and_page_cap_never_mark_complete(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, api_graph([1, 2]))), patch.object(listing, "listing_retry_delay", return_value=0), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(runner.DryerError, "repeated_organic_page"):
                runner.collect_listing(self.runtime, Path(directory), "main", 5, 20)
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", return_value=(200, api_graph([1, 2]))), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(runner.DryerError, "max_pages_reached"):
                runner.collect_listing(self.runtime, Path(directory), "main", 1, 0)

    def test_shared_ldy_transport_and_features_batch_contract(self):
        response = [{"data": {"productBySkuId": api_product(i)}} for i in (1, 2)]
        with patch.object(ldy, "browser_graphql_post", return_value=(200, "unused", response, {}, 0)) as legacy:
            captured, errors = runner.collect_product_batch(self.runtime, [list_row(1, 1), list_row(2, 2)])
        self.assertFalse(errors)
        self.assertEqual(set(captured), {"1", "2"})
        payloads = legacy.call_args.args[0]
        self.assertEqual(len(payloads), 2)
        self.assertIn("features{description title}", payloads[0]["query"])
        for field in ("fulfillmentOptions", "reviews(", "buyingOptions", "GetCompareProduct"):
            self.assertNotIn(field, payloads[0]["query"])
        self.assertEqual(payloads[0]["variables"]["skuId"], "1")

    def test_failed_listing_pass_does_not_splice_earlier_pages(self):
        graphs = [(200, api_graph([1, 2])), (200, {"data": {}}),
                  (200, api_graph([3, 4])), (200, api_graph([5, 6]))]
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "load_product_list_operation", return_value=self.operation), patch.object(runner, "api_post", side_effect=graphs), patch.object(listing, "listing_retry_delay", return_value=0), redirect_stdout(io.StringIO()):
            rows = runner.collect_listing(self.runtime, Path(directory), "main", 5, 4)
        self.assertEqual([r["sku_id"] for r in rows], ["3", "4", "5", "6"])

    def test_batch_identity_mismatch_and_missing_features_fail(self):
        wrong = [{"data": {"productBySkuId": api_product(i)}} for i in (2, 1)]
        with patch.object(runner, "api_post", return_value=(200, wrong)):
            captured, errors = runner.collect_product_batch(self.runtime, [list_row(1, 1), list_row(2, 2)])
        self.assertFalse(captured)
        self.assertEqual(set(errors.values()), {"detail_product_identity_mismatch"})
        p = api_product(1)
        del p["features"]
        with patch.object(runner, "api_post", return_value=(200, [{"data": {"productBySkuId": p}}])):
            captured, errors = runner.collect_product_batch(self.runtime, [list_row(1, 1)])
        self.assertEqual(errors["1"], "detail_attribute_response_incomplete")

    def test_only_failed_sku_retried_and_success_cached(self):
        calls = []
        def post(_, payloads):
            skus = [p["variables"]["skuId"] for p in payloads]
            calls.append(skus)
            return 200, [{"errors": [{"message": "temporary synthetic failure"}]} if sku == "2" and len(calls) == 1
                         else {"data": {"productBySkuId": api_product(sku)}} for sku in skus]
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "api_post", side_effect=post), patch.object(ldy, "MAX_ATTEMPTS", 2), patch.object(ldy, "detail_retry_sleep_seconds", return_value=0), redirect_stdout(io.StringIO()):
            root = Path(directory)
            targets = rules.merge_targets([list_row(1, 1), list_row(2, 2)], [])
            rows, _, failures = runner.collect_details(self.runtime, root, targets, "b_test", 5)
            self.assertTrue((root / "products" / "1.json").exists())
            with patch.object(runner, "collect_product_batch") as capture:
                cached, _, _ = runner.collect_details(self.runtime, root, targets, "b_test", 5)
                capture.assert_not_called()
        self.assertEqual(calls, [["1", "2"], ["2"]])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows, cached)
        self.assertFalse(failures)

    def test_main20_bsr10_complete_run_counts_overlap_and_load(self):
        import json
        main = [list_row(i, i) for i in range(1, 21)]
        bsr = [list_row(i, n) for n, i in enumerate(range(16, 26), 1)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.write_json(root / "dryer_manifest.json", {"collector_version":2, "main_limit":20, "bsr_limit":10, "batch_id":"b_existing"})
            def captures(_, targets):
                return {t["sku_id"]: {"collector_version":2, "product":api_product(t["sku_id"]), "captured_at":"2026-10-08T10:00:00"} for t in targets}, {}
            with patch.object(runner, "load_runtime", return_value=self.runtime), patch.object(runner, "collect_listing", side_effect=[main, bsr]), patch.object(runner, "connect_db", return_value=FakeConnection(FakeCursor())), patch.object(runner, "collect_product_batch", side_effect=captures) as fetch, patch.object(runner, "load_test_table", return_value=25) as load, redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume", str(root)]), 0)
            result = json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((result["main_target_count"], result["bsr_target_count"], result["overlap_count"], result["collected_count"]), (20, 10, 5, 25))
            self.assertEqual(fetch.call_count, 5)
            self.assertEqual(len(load.call_args.args[1]), 25)

    def test_old_render_run_cannot_resume_as_api_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = {"limit":10, "batch_id":"b_old"}
            runner.write_json(root / "dryer_manifest.json", old)
            with patch.object(runner, "load_runtime") as runtime, redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--resume", str(root)]), 1)
            runtime.assert_not_called()
            import json
            self.assertEqual(json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8")), old)


class LoggingTests(unittest.TestCase):
    def test_legacy_first_import_stdout_rewrap_preserves_console_and_privacy(self):
        import sys
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as console:
            root=Path(directory)
            with diagnostics.run_logging(root):
                with diagnostics.safe_legacy_output():
                    sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
                    print("synthetic_private_value",flush=True)
                    diagnostics.event("safe_marker",attempt=1)
                diagnostics.event("console_after_import",status="ok")
            text=(root/"logs/dryer.log").read_text(encoding="utf-8")
            self.assertIn("console_after_import",console.getvalue())
            self.assertNotIn("synthetic_private_value",console.getvalue()+text)

    def test_final_failure_stage_tracks_last_retry_and_preserves_leaf(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with diagnostics.run_logging(Path(directory)) as logger:
                try:
                    with diagnostics.phase("chrome_start"):
                        raise RuntimeError("initial synthetic failure")
                except RuntimeError:
                    pass
                try:
                    with diagnostics.phase("browser_api"):
                        try:
                            with diagnostics.phase("page_verification"):
                                raise TimeoutError("later synthetic failure")
                        except TimeoutError as cause:
                            raise runner.DryerError("browser_api_unavailable") from cause
                except runner.DryerError:
                    pass
                self.assertEqual(logger.failure_stage,"page_verification")

    def test_console_text_and_json_report_failure_without_raw_message(self):
        import json
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as console:
            root = Path(directory)
            with diagnostics.run_logging(root) as logger:
                try:
                    with diagnostics.phase("site_navigation"):
                        raise RuntimeError("access denied; synthetic_private_value")
                except RuntimeError:
                    pass
                self.assertEqual(logger.failure_stage, "site_navigation")
            text = (root / "logs/dryer.log").read_text(encoding="utf-8")
            events = [json.loads(line) for line in (root / "logs/dryer_events.jsonl").read_text(encoding="utf-8").splitlines()]
            failure = next(e for e in events if e["event"] == "stage_failed")
            self.assertEqual(failure["error_category"], "site_access_denied")
            self.assertTrue(failure["trace"])
            self.assertNotIn("synthetic_private_value", text + console.getvalue() + str(events))
            self.assertIn("site_navigation", console.getvalue())

    def test_waiting_heartbeat_appears_during_blocked_phase(self):
        import threading
        emitted = threading.Event()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            logger = diagnostics.RunLogger(Path(directory), heartbeat_seconds=0.01)
            original = logger.emit
            def observe(name, **fields):
                original(name, **fields)
                if name == "waiting":
                    emitted.set()
            try:
                with patch.object(diagnostics, "_LOGGER", logger), patch.object(logger, "emit", side_effect=observe):
                    with diagnostics.phase("graphql_fetch"):
                        self.assertTrue(emitted.wait(timeout=1))
            finally:
                logger.close()
            import json
            events = [json.loads(line) for line in logger.json_path.read_text(encoding="utf-8").splitlines()]
            wait = next(e for e in events if e["event"] == "waiting")
            self.assertEqual(wait["stage"], "graphql_fetch")
            self.assertGreaterEqual(wait["wait_s"], 0)

    def test_browser_instrumentation_restores_helper_after_exception(self):
        def broken():
            raise TimeoutError("synthetic_private_value")
        helper = SimpleNamespace(fetch_detail_browser_graphql_envelope=broken)
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with diagnostics.run_logging(Path(directory)) as logger:
                with self.assertRaises(TimeoutError), diagnostics.trace_browser_calls(helper):
                    helper.fetch_detail_browser_graphql_envelope()
                self.assertIs(helper.fetch_detail_browser_graphql_envelope, broken)
                self.assertEqual(logger.failure_stage, "graphql_fetch")

    def test_legacy_output_is_discarded_including_injected_progress_lines(self):
        import sys
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as console:
            root = Path(directory)
            with diagnostics.run_logging(root), diagnostics.safe_legacy_output():
                print("[detail:browser_bootstrap] attempt=123 error=synthetic_private_value")
                print("synthetic_private_value", file=sys.stderr)
                diagnostics.event("safe_marker", attempt=1)
            output = console.getvalue() + (root / "logs/dryer.log").read_text(encoding="utf-8")
            self.assertNotIn("synthetic_private_value", output)
            self.assertNotIn("attempt=123", output)
            self.assertIn("safe_marker", output)

    def test_graphql_error_summary_has_only_known_categories_and_fields(self):
        result = diagnostics.graphql_diagnostic([{"message":'Cannot query field "features" on type "Product". synthetic_private_value',
                                                  "extensions":{"unexpected":"synthetic_private_value"}}])
        self.assertEqual(result["graphql_error_categories"], ["unsupported_field"])
        self.assertEqual(result["graphql_fields"], ["features"])
        self.assertNotIn("synthetic_private_value", str(result))

    def test_arbitrary_value_error_message_is_not_an_error_reason(self):
        self.assertEqual(runner.safe_reason(ValueError("synthetic_private_value")), "ValueError")

    def test_runtime_headless_matches_existing_ldy_visible_browser(self):
        attributes = {"CATEGORY": listing.CATEGORY, "SEARCH_TERM": listing.SEARCH_TERM,
                      "SEARCH_URL_TEMPLATE": listing.SEARCH_URL_TEMPLATE,
                      "INCLUDE_SPONSORED_CAROUSEL": listing.INCLUDE_SPONSORED_CAROUSEL,
                      "BROWSER_GRAPHQL_HEADLESS": listing.BROWSER_GRAPHQL_HEADLESS}
        with patch.dict(os.environ, {}), patch.multiple(listing, **attributes), patch.object(ldy, "BROWSER_GRAPHQL_HEADLESS", True):
            runtime = runner.load_runtime(Path("unused_offline_path"))
            self.assertFalse(runtime[1].BROWSER_GRAPHQL_HEADLESS)
            self.assertFalse(runtime[2].BROWSER_GRAPHQL_HEADLESS)
            self.assertEqual(os.environ["BESTBUY_DETAIL_BROWSER_GRAPHQL_HEADLESS"], "0")

    def test_inherited_existing_run_paths_and_explicit_ports_are_not_used(self):
        attributes = {"CATEGORY": listing.CATEGORY, "SEARCH_TERM": listing.SEARCH_TERM,
                      "SEARCH_URL_TEMPLATE": listing.SEARCH_URL_TEMPLATE,
                      "INCLUDE_SPONSORED_CAROUSEL": listing.INCLUDE_SPONSORED_CAROUSEL,
                      "BROWSER_GRAPHQL_HEADLESS": listing.BROWSER_GRAPHQL_HEADLESS}
        root=Path("dedicated_dryer_run")
        inherited={"BESTBUY_OUTPUT_ROOT":"existing_category_output","BESTBUY_DETAIL_RUN_ROOT":"existing_category_detail",
                   "BESTBUY_FINAL_OUTPUT_CSV":"existing_category.csv","BESTBUY_DETAIL_TARGET_CSV":"existing_targets.csv",
                   "BESTBUY_BROWSER_GRAPHQL_LOCAL_PORT":"1234","BESTBUY_DETAIL_BROWSER_GRAPHQL_LOCAL_PORT":"1234"}
        with patch.dict(os.environ,inherited),patch.multiple(listing,**attributes),patch.object(ldy,"BROWSER_GRAPHQL_HEADLESS",True):
            runner.load_runtime(root)
            self.assertEqual(os.environ["BESTBUY_OUTPUT_ROOT"],str(root/"output"))
            self.assertEqual(os.environ["BESTBUY_DETAIL_RUN_ROOT"],str(root/"detail"))
            self.assertEqual(os.environ["BESTBUY_FINAL_OUTPUT_CSV"],str(root/"output/final_output.csv"))
            self.assertEqual(os.environ["BESTBUY_DETAIL_TARGET_CSV"],str(root/"output/bestbuy_final_targets.csv"))
            self.assertEqual(os.environ["BESTBUY_DETAIL_BROWSER_GRAPHQL_LOCAL_PORT"],"0")

    def test_first_api_failure_records_real_stage_and_exception_chain(self):
        import json
        config = SimpleNamespace(bestbuy_zip_code=lambda:"10010", bestbuy_store_id=lambda:"482")
        def fail_navigation(*args, **kwargs):
            raise RuntimeError("navigation returned false: access denied synthetic_private_value")
        def post(*args, **kwargs):
            ldy.navigate_detail_browser("unused_public_url", "home")
        def listing_failure(runtime, *args):
            runner.api_post(runtime[2], {"operationName":"PublicProbe"})
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "load_runtime", return_value=(config, listing, ldy)), patch.object(runner, "collect_listing", side_effect=listing_failure), patch.object(ldy, "browser_graphql_post", side_effect=post), patch.object(ldy, "navigate_detail_browser", side_effect=fail_navigation), redirect_stdout(io.StringIO()):
            root = Path(directory)
            runner.write_json(root / "dryer_manifest.json", {"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            original = ldy.navigate_detail_browser
            self.assertEqual(runner.main(["--resume", str(root), "--no-load"]), 1)
            self.assertIs(ldy.navigate_detail_browser, original)
            result = json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["failure_stage"], "site_navigation")
            self.assertEqual(result["error"], "browser_api_unavailable")
            self.assertEqual(result["error_diagnostics"]["error_category"], "site_access_denied")
            self.assertFalse(result["db_loaded"])
            self.assertNotIn("synthetic_private_value", (root / "logs/dryer.log").read_text(encoding="utf-8") + str(result))

    def test_db_preflight_failure_is_separate_from_api_failure(self):
        import json
        config = SimpleNamespace(bestbuy_zip_code=lambda:"10010", bestbuy_store_id=lambda:"482")
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "load_runtime", return_value=(config, listing, ldy)), patch.object(runner, "connect_db", side_effect=RuntimeError("synthetic_private_value")), patch.object(runner, "collect_listing") as collect, redirect_stdout(io.StringIO()):
            root = Path(directory)
            runner.write_json(root / "dryer_manifest.json", {"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            self.assertEqual(runner.main(["--resume",str(root)]),1)
            collect.assert_not_called()
            result=json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["failure_stage"],"db_preflight")
            self.assertNotIn("synthetic_private_value",str(result))

    def test_required_column_failure_records_missing_field_names(self):
        import json
        config=SimpleNamespace(bestbuy_zip_code=lambda:"10010",bestbuy_store_id=lambda:"482")
        p=api_product(1)
        p["price"]={}
        captured={"collector_version":2,"product":p,"captured_at":"2026-10-08T10:00:00"}
        with tempfile.TemporaryDirectory() as directory, patch.object(runner,"load_runtime",return_value=(config,listing,ldy)), patch.object(runner,"collect_listing",side_effect=[[list_row(1,1)],[]]), patch.object(runner,"collect_product_batch",return_value=({"1":captured},{})), patch.object(ldy,"MAX_ATTEMPTS",1), redirect_stdout(io.StringIO()):
            root=Path(directory)
            runner.write_json(root/"dryer_manifest.json",{"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            self.assertEqual(runner.main(["--resume",str(root),"--no-load"]),1)
            failures=json.loads((root/"output/failures.json").read_text(encoding="utf-8"))
            self.assertEqual(failures[0]["missing_fields"],["final_sku_price"])
            text=(root/"logs/dryer.log").read_text(encoding="utf-8")
            self.assertIn("db_load_skipped",text)
            self.assertIn("final_sku_price",text)

    def test_progress_percentage_and_unknown_total(self):
        import json
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root=Path(directory)
            with diagnostics.run_logging(root):
                runner.progress("detail",3,10)
                runner.progress("main_list",8,0)
            events=[json.loads(line) for line in (root/"logs/dryer_events.jsonl").read_text(encoding="utf-8").splitlines()]
            entries=[e for e in events if e["event"]=="progress"]
            self.assertEqual(entries[0]["progress_pct"],30)
            self.assertIsNone(entries[1]["progress_pct"])

    def test_interruption_is_recorded_and_returns_130(self):
        import json
        config=SimpleNamespace(bestbuy_zip_code=lambda:"10010",bestbuy_store_id=lambda:"482")
        with tempfile.TemporaryDirectory() as directory, patch.object(runner,"load_runtime",return_value=(config,listing,ldy)), patch.object(runner,"collect_listing",side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            root=Path(directory)
            runner.write_json(root/"dryer_manifest.json",{"collector_version":2,"main_limit":20,"bsr_limit":10,"batch_id":"b_existing"})
            self.assertEqual(runner.main(["--resume",str(root),"--no-load"]),130)
            result=json.loads((root/"dryer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"],"interrupted")
            self.assertEqual(result["failure_stage"],"main_listing")


if __name__ == "__main__":
    unittest.main()
