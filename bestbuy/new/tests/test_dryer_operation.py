"""Offline operational DB isolation and DRYER notifications; no external I/O."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_dryer import FakeCursor, FakeConnection, ldy, listing, product, rules, runner, target
from bestbuy import step18_dryer_email_notify as notify
from bestbuy import step16_email_notify as mail


class TransactionCursor(FakeCursor):
    def __init__(self, records, wrong_count=False, insert_error=False):
        super().__init__()
        self.records = records
        self.wrong_count = wrong_count
        self.insert_error = insert_error

    def execute(self, sql, values=None):
        super().execute(sql, values)
        self.last_sql, self.last_values = sql, values
        if sql.startswith("DELETE"):
            batch, account = values
            self.records[:] = [r for r in self.records
                               if (r["batch_id"], r["account_name"]) != (batch, account)]

    def executemany(self, sql, values):
        super().executemany(sql, values)
        if self.insert_error:
            raise RuntimeError("simulated_insert_failure")
        self.records.extend(dict(zip(rules.FIELDS[1:], row)) for row in values)

    def fetchone(self):
        batch, account = self.last_values
        count = sum((r["batch_id"], r["account_name"]) == (batch, account) for r in self.records)
        return (0 if self.wrong_count else count,)


class TransactionConnection(FakeConnection):
    def __enter__(self):
        self.original = copy.deepcopy(self.c.records)
        self.rolled_back = False
        return self

    def __exit__(self, kind, value, traceback):
        if kind:
            self.c.records[:] = self.original
            self.rolled_back = True
        return False


class OperationalDatabaseTests(unittest.TestCase):
    def test_repeated_batch_preserves_costco_and_previous_bestbuy_batches(self):
        records = [{"account_name": account, "batch_id": batch, "item": item}
                   for account, batch, item in (("Costco", "b_test", "costco"),
                                                ("Bestbuy", "b_old", "previous"),
                                                ("Bestbuy", "b_test", "obsolete"))]
        preserved = copy.deepcopy(records[:2])
        cursor = TransactionCursor(records)
        row = rules.make_row(target(), product(), ldy, "b_test", datetime.now())[0]
        with patch.object(runner, "connect_db", side_effect=lambda _: TransactionConnection(cursor)):
            self.assertEqual(runner.load_table(None, [row], "b_test"), 1)
            self.assertEqual(runner.load_table(None, [row], "b_test"), 1)
        self.assertEqual(records[:2], preserved)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[-1]["item"], row["item"])
        writes = [(sql, params) for sql, params in cursor.calls if sql.startswith(("DELETE", "SELECT count"))]
        self.assertTrue(all("public.ldy_dryer_retail " in sql for sql, _ in writes))
        self.assertTrue(all(params == ("b_test", "Bestbuy") for _, params in writes))
        self.assertFalse(any(sql.startswith(("CREATE", "ALTER", "DROP", "TRUNCATE")) for sql, _ in cursor.calls))

    def test_insert_or_count_failure_rolls_back_same_batch_replacement(self):
        for wrong_count, insert_error in ((True, False), (False, True)):
            with self.subTest(wrong_count=wrong_count, insert_error=insert_error):
                records = [{"account_name": "Bestbuy", "batch_id": "b_test", "item": "original"}]
                original = copy.deepcopy(records)
                cursor = TransactionCursor(records, wrong_count, insert_error)
                connection = TransactionConnection(cursor)
                row = rules.make_row(target(), product(), ldy, "b_test", datetime.now())[0]
                with patch.object(runner, "connect_db", return_value=connection):
                    with self.assertRaises((runner.DryerError, RuntimeError)):
                        runner.load_table(None, [row], "b_test")
                self.assertEqual(records, original)
                self.assertTrue(connection.rolled_back)
                self.assertTrue(connection.closed)

    def test_other_account_is_rejected_before_connection(self):
        row = rules.make_row(target(), product(), ldy, "b_test", datetime.now())[0]
        row["account_name"] = "Costco"
        with patch.object(runner, "connect_db") as connect:
            with self.assertRaisesRegex(runner.DryerError, "invalid_dryer_batch"):
                runner.load_table(None, [row], "b_test")
            connect.assert_not_called()

    def test_schema_mismatch_is_rejected_before_delete(self):
        for mode in ("missing", "extra"):
            class Mismatch(FakeCursor):
                def fetchall(self):
                    fields = super().fetchall()
                    return fields[:-1] if mode == "missing" else fields + [("unexpected", "text", False, "NO")]
            cursor = Mismatch()
            row = rules.make_row(target(), product(), ldy, "b_test", datetime.now())[0]
            with self.subTest(mode=mode), patch.object(runner, "connect_db", return_value=FakeConnection(cursor)):
                with self.assertRaisesRegex(runner.DryerError, "exactly_20"):
                    runner.load_table(None, [row], "b_test")
            self.assertFalse(any(sql.startswith(("DELETE", "INSERT", "ALTER")) for sql, _ in cursor.calls))


def manifest(**changes):
    value = {"status": "success", "collection_status": "complete", "db_loaded": True,
             "table": rules.TABLE, "batch_id": "b_test", "main_limit": 2, "bsr_limit": 1,
             "main_target_count": 2, "bsr_target_count": 1, "overlap_count": 1,
             "target_count": 2, "collected_count": 2, "output_count": 2, "inserted_count": 2,
             "failure_count": 0, "fallback_count": 0, "elapsed_s": 138.08,
             "null_counts": {"loading_type": 2, "capacity": 1}}
    return dict(value, **changes)


class DryerNotificationTests(unittest.TestCase):
    def test_complete_run_with_expected_nulls_uses_existing_bby_subject_and_summary(self):
        payload = notify.build_notification(manifest(), Path("public_run"))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["subject"], mail.build_subject("DRYER", []))
        for phrase in ("b_test", "public.ldy_dryer_retail", "메인: 2 / 2", "BSR: 1 / 1", "중복: 1",
                       "최종 출력: 2", "DB 적재: 2", "138.08", "dryer.log"):
            self.assertIn(phrase, payload["body"])

    def test_shortfall_fallback_partial_and_db_failure_are_not_success(self):
        cases = ((manifest(main_target_count=1), "partial"),
                 (manifest(status="success_with_warnings", failure_count=1), "partial"),
                 (manifest(status="failed", collection_status="partial", db_loaded=False, output_count=1), "partial"),
                 (manifest(status="failed", db_loaded=False, failure_stage="db_load"), "failed"),
                 (manifest(db_loaded=False), "failed"))
        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(notify.notification_status(value), expected)

    def test_existing_settings_and_transport_are_used_without_persisting_configuration(self):
        settings = {"simulated_settings": True}
        with patch.dict(os.environ, {"BESTBUY_EMAIL_NOTIFY": "1", "BESTBUY_EMAIL_DRY_RUN": "0"}), \
                patch.object(mail, "email_config", return_value=settings) as configure, \
                patch.object(mail, "send_email", return_value=(True, "")) as send, redirect_stdout(io.StringIO()):
            result = notify.notify_run(manifest(), Path("public_run"))
        configure.assert_called_once_with()
        self.assertIs(send.call_args.args[2], settings)
        self.assertTrue(result["sent"])
        self.assertNotIn("simulated_settings", json.dumps(result))
        self.assertNotIn("body", result)

    def test_disabled_and_dry_run_do_not_load_settings_or_send(self):
        for flags in ({"BESTBUY_EMAIL_NOTIFY": "0", "BESTBUY_EMAIL_DRY_RUN": "0"},
                      {"BESTBUY_EMAIL_NOTIFY": "1", "BESTBUY_EMAIL_DRY_RUN": "1"}):
            with self.subTest(flags=flags), patch.dict(os.environ, flags), \
                    patch.object(mail, "email_config") as configure, patch.object(mail, "send_email") as send, \
                    redirect_stdout(io.StringIO()):
                result = notify.notify_run(manifest(), Path("public_run"))
            self.assertEqual(result["status"], "skipped")
            configure.assert_not_called()
            send.assert_not_called()

    def test_mail_exception_and_skip_reason_do_not_leak_raw_error_text(self):
        for response in (RuntimeError("synthetic_private_value"), (False, "synthetic_private_value"),
                         (False, "email config missing: smtp_server")):
            kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with self.subTest(response_type=type(response).__name__), \
                    patch.dict(os.environ, {"BESTBUY_EMAIL_NOTIFY": "1", "BESTBUY_EMAIL_DRY_RUN": "0"}), \
                    patch.object(mail, "email_config", return_value={}), patch.object(mail, "send_email", **kwargs), \
                    redirect_stdout(io.StringIO()) as output:
                result = notify.notify_run(manifest(), Path("public_run"))
            self.assertFalse(result["sent"])
            self.assertNotIn("synthetic_private_value", str(result) + output.getvalue())


class DryerOperationalFlowTests(unittest.TestCase):
    def run_case(self, root, *, no_load=False, notify_effect=None, missing_table=False, db_error=False):
        config = SimpleNamespace(bestbuy_zip_code=lambda: "10010", bestbuy_store_id=lambda: "482")
        cursor = FakeCursor()
        if missing_table:
            cursor.fetchall = lambda: []
        runner.write_json(root / "dryer_manifest.json", {"collector_version": runner.COLLECTOR_VERSION,
                          "main_limit": 1, "bsr_limit": 1, "batch_id": "b_test", "table": rules.TABLE})
        rows = [dict(target(), product_name="Electric Dryer", container_type="organic_product")]
        capture = {"collector_version": runner.COLLECTOR_VERSION, "product": product(),
                   "captured_at": "2026-10-12T03:11:09"}
        kwargs = {"side_effect": notify_effect} if notify_effect else {"return_value": {"sent": True, "status": "sent"}}
        with patch.object(runner, "load_runtime", return_value=(config, listing, ldy)), \
                patch.object(runner, "connect_db", side_effect=lambda *a, **k: FakeConnection(cursor)), \
                patch.object(runner, "collect_listing", side_effect=[rows, rows]) as collect, \
                patch.object(runner, "collect_product_batch", return_value=({"6471411": capture}, {})), \
                patch.object(notify, "notify_run", **kwargs) as send, redirect_stdout(io.StringIO()):
            with patch.object(runner, "load_table", side_effect=runner.DryerError("simulated_db_load_failure")) if db_error else patch.object(runner, "load_table", wraps=runner.load_table):
                code = runner.main(["--main-limit", "1", "--bsr-limit", "1", "--resume", str(root)]
                                   + (["--no-load"] if no_load else []))
        result = json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (root / "logs/dryer_events.jsonl").read_text(encoding="utf-8").splitlines()]
        return code, result, events, send, collect

    def test_success_sends_after_db_confirmation_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            code, result, events, send, _ = self.run_case(Path(directory))
            self.assertEqual(code, 0)
            self.assertTrue(send.call_args.args[0]["db_loaded"])
            self.assertEqual(send.call_args.args[0]["inserted_count"], 1)
            self.assertEqual(result["table"], "public.ldy_dryer_retail")
            self.assertTrue(result["email"]["sent"])
            self.assertGreaterEqual(result["total_elapsed_s"], result["elapsed_s"])
            self.assertTrue((Path(directory) / "output/email_notify_manifest.json").is_file())
        steps = [e.get("stage") for e in events if e["event"] == "stage_complete"]
        self.assertLess(steps.index("db_load"), steps.index("email_notify"))
        self.assertLess(steps.index("browser_cleanup"), steps.index("email_notify"))

    def test_mail_error_does_not_change_collected_result_or_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            code, result, events, _, _ = self.run_case(Path(directory), notify_effect=RuntimeError("synthetic_private_value"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["db_loaded"])
        self.assertEqual(result["email"]["status"], "failed")
        self.assertNotIn("synthetic_private_value", str(result) + str(events))

    def test_missing_table_or_load_error_is_reported_and_not_success(self):
        for flags, expected in (({"missing_table": True}, "db_preflight"), ({"db_error": True}, "db_load")):
            with self.subTest(stage=expected), tempfile.TemporaryDirectory() as directory:
                code, result, _, send, collect = self.run_case(Path(directory), **flags)
            self.assertEqual(code, 1)
            self.assertFalse(result["db_loaded"])
            self.assertEqual(result["failure_stage"], expected)
            send.assert_called_once()
            if flags.get("missing_table"):
                collect.assert_not_called()

    def test_no_load_does_not_notify_and_mail_interrupt_returns_130(self):
        with tempfile.TemporaryDirectory() as directory:
            code, result, _, send, _ = self.run_case(Path(directory), no_load=True)
        self.assertEqual(code, 0)
        send.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            code, result, _, _, _ = self.run_case(Path(directory), notify_effect=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertTrue(result["db_loaded"])
        self.assertEqual(result["email"]["status"], "interrupted")

    def test_test_table_resume_is_rejected_before_runtime_and_original_manifest_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"collector_version": runner.COLLECTOR_VERSION, "table": "public.ldy_dryer_retail_test",
                        "main_limit": 300, "bsr_limit": 100, "batch_id": "b_old", "status": "success"}
            runner.write_json(root / "dryer_manifest.json", original)
            with patch.object(runner, "load_runtime") as runtime, redirect_stdout(io.StringIO()):
                code = runner.main(["--resume", str(root), "--no-load"])
            self.assertEqual(code, 1)
            runtime.assert_not_called()
            self.assertEqual(json.loads((root / "dryer_manifest.json").read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
