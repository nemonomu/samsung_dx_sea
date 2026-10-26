"""Offline mail, production-table and inert Windows BAT integration checks."""
from contextlib import redirect_stdout
import copy
from datetime import datetime
import inspect
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from . import lowes_dryer as collector
from . import step14_dryer_db_load as loader
from . import step15_dryer_email_notify as mail
from .step00_dryer import DEFAULT_TABLE, PRODUCTION_TABLE, TEST_TABLE, Settings
from .test_dryer import final_row, scratch


REPO = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger("lowes.dryer.operations-test")
LOGGER.addHandler(logging.NullHandler())


def manifest(status="complete", loaded=True):
    return {"batch_id": "l_test", "table": PRODUCTION_TABLE, "status": status,
            "started_at": "2026-10-15T01:00:00", "finished_at": "2026-10-15T01:06:02",
            "db_loaded": loaded, "collect_only": False,
            "collection": {"counts": {"rows": 233, "main": 229, "bsr": 88, "shared": 84},
                           "api_failure_products": 0, "quality_warnings": 0},
            "db": {"table": PRODUCTION_TABLE, "inserted": 233, "verified_count": 233} if loaded else {}}


class MailTests(unittest.TestCase):
    def test_complete_below_limits_is_not_a_warning_and_uses_actual_counts(self):
        notification = mail.build_notification(manifest())
        self.assertFalse(notification["issues"])
        self.assertEqual(notification["subject"], "[SEA] Lowes DRYER crawled")
        for text in (PRODUCTION_TABLE, "233", "229", "88", "84", "362.0"):
            self.assertIn(text, notification["body"])
        self.assertNotIn("ZenRows", notification["body"])

    def test_partial_failed_interrupted_and_collect_only_are_distinct(self):
        for status, expected in (("partial_failure", "일부 실패"), ("failed", "실패"), ("interrupted", "중단")):
            n = mail.build_notification(manifest(status, loaded=status == "partial_failure"))
            self.assertIn("[Warning]", n["subject"])
            self.assertIn(expected, n["body"])
            self.assertNotIn("정상 완료", n["body"])
        m = manifest(loaded=False)
        m["collect_only"] = True
        n = mail.build_notification(m)
        self.assertIn("[Collect only]", n["subject"])
        self.assertIn("DB 미적재", n["body"])

    def test_unconfirmed_db_and_wrong_actual_table_are_warning(self):
        unconfirmed = mail.build_notification(manifest(loaded=False))
        self.assertIn("db_load_unconfirmed", unconfirmed["issues"])
        self.assertNotIn("정상 완료", unconfirmed["body"])
        m = manifest()
        m["db"]["table"] = TEST_TABLE
        n = mail.build_notification(m)
        self.assertIn(TEST_TABLE, n["body"])
        self.assertIn("db_table_mismatch", n["issues"])
        self.assertNotIn("정상 완료", n["body"])

    def test_missing_counts_are_unknown_not_zero_and_no_targets_is_normal(self):
        n = mail.build_notification({"status": "failed", "table": PRODUCTION_TABLE})
        self.assertIn("수집 상품: 미확인", n["body"])
        self.assertIn("확인된 DB 적재: 미확인", n["body"])
        m = manifest(loaded=False)
        m["collection"]["counts"] = dict.fromkeys(("rows", "main", "bsr", "shared"), 0)
        m["db_skip_reason"] = "no_eligible_products"
        self.assertFalse(mail.build_notification(m)["issues"])

    def test_reuses_existing_lowes_config_and_sender_without_copying_settings(self):
        opaque = object()
        config = MagicMock(return_value=opaque)
        sender = MagicMock(return_value=(True, ""))
        with patch.dict(sys.modules, {"lowes.step15_email_notify": SimpleNamespace(email_config=config, send_email=sender)}):
            self.assertTrue(mail._send_existing_lowes("subject", "body"))
        sender.assert_called_once_with("subject", "body", opaque)

    def test_same_run_sends_once_and_does_not_persist_settings(self):
        with scratch() as root:
            sender = MagicMock(return_value=True)
            first = mail.notify_run(root, manifest(), LOGGER, sender, {})
            second = mail.notify_run(root, manifest(), LOGGER, sender, {})
            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "skipped")
            sender.assert_called_once()
            saved = json.loads((root/"output/email_notify_manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("from", saved)
            self.assertNotIn("to", saved)
            self.assertNotIn("config", saved)
            self.assertFalse((root/"output/email_notify_manifest.lock").exists())

    def test_uncertain_delivery_never_auto_retries_or_exposes_exception(self):
        with scratch() as root:
            sender = MagicMock(side_effect=RuntimeError("PRIVATE_ERROR_PAYLOAD"))
            m = manifest()
            before = copy.deepcopy(m)
            result = mail.notify_run(root, m, LOGGER, sender, {})
            self.assertEqual(result["status"], "delivery_unknown")
            mail.notify_run(root, m, LOGGER, sender, {})
            sender.assert_called_once()
            self.assertEqual(m, before)
            self.assertNotIn("PRIVATE_ERROR_PAYLOAD", (root/"output/email_notify_manifest.json").read_text(encoding="utf-8"))

    def test_disabled_dry_run_and_lock_prevent_sending(self):
        for env in ({"LOWES_EMAIL_NOTIFY": "0"}, {"LOWES_EMAIL_DRY_RUN": "1"}):
            with scratch() as root:
                sender = MagicMock()
                self.assertEqual(mail.notify_run(root, manifest(), LOGGER, sender, env)["status"], "skipped")
                sender.assert_not_called()
        with scratch() as root:
            (root/"output").mkdir()
            (root/"output/email_notify_manifest.lock").touch()
            sender = MagicMock()
            result = mail.notify_run(root, manifest(), LOGGER, sender, {})
            self.assertEqual(result["reason"], "notification_locked")
            sender.assert_not_called()


class TableAndRunTests(unittest.TestCase):
    def test_production_defaults_explicit_test_and_allowlist(self):
        self.assertEqual(DEFAULT_TABLE, PRODUCTION_TABLE)
        self.assertEqual(Settings().target_table, PRODUCTION_TABLE)
        self.assertEqual(Settings.from_env({"LOWES_DRYER_TABLE": TEST_TABLE}).target_table, TEST_TABLE)
        self.assertEqual(inspect.signature(loader.load_batch).parameters["table"].default, PRODUCTION_TABLE)
        self.assertEqual(inspect.signature(loader.verify_batch).parameters["table"].default, PRODUCTION_TABLE)
        with self.assertRaises(ValueError):
            Settings.from_env({"LOWES_DRYER_TABLE": "public.ldy_retail_com"})

    def test_csv_table_default_overrides_and_historical_test_metadata(self):
        with scratch() as root:
            csv = root/"output/final_output.csv"
            csv.parent.mkdir()
            self.assertEqual(loader.table_for_csv(csv, "l_test"), PRODUCTION_TABLE)
            self.assertEqual(loader.table_for_csv(csv, "l_test", env={"LOWES_DRYER_TABLE": TEST_TABLE}), TEST_TABLE)
            self.assertEqual(loader.table_for_csv(csv, "l_test", PRODUCTION_TABLE, {"LOWES_DRYER_TABLE": TEST_TABLE}), PRODUCTION_TABLE)
            (root/"collection_summary.json").write_text(json.dumps({"batch_id": "l_test", "settings": {}}))
            self.assertEqual(loader.table_for_csv(csv, "l_test"), TEST_TABLE)
            (root/"manifest.json").write_text(json.dumps({"batch_id": "l_test", "table": PRODUCTION_TABLE}))
            self.assertEqual(loader.table_for_csv(csv, "l_test"), PRODUCTION_TABLE)
            (root/"db_load_summary.json").write_text(json.dumps({"batch_id": "l_test", "table": TEST_TABLE}))
            with self.assertRaises(loader.DryerDBError):
                loader.table_for_csv(csv, "l_test")
            self.assertEqual(loader.table_for_csv(csv, "l_test", TEST_TABLE), TEST_TABLE)
            with self.assertRaises(loader.DryerDBError):
                loader.table_for_csv(csv, "different_batch")

    def test_loader_and_verify_cli_share_selected_table_without_live_config(self):
        for verify in (False, True):
            for table in (PRODUCTION_TABLE, TEST_TABLE):
                fn = "verify_batch" if verify else "load_batch"
                args = ["--csv", "offline.csv", "--table", table] + (["--verify-only"] if verify else [])
                with patch.object(loader, "read_rows", return_value=[final_row()]), \
                     patch.object(loader, "runtime_table", return_value=table) as resolve, \
                     patch.object(loader, fn, return_value={"table": table}) as operation, redirect_stdout(io.StringIO()):
                    self.assertEqual(loader.main(args), 0)
                resolve.assert_called_once_with(Path("offline.csv"), "l_test", table)
                operation.assert_called_once_with([final_row()], table=table, allow_production=table == PRODUCTION_TABLE)

    def test_unprovided_required_table_column_fails_before_any_delete(self):
        from .test_dryer import DBTests
        conn, cur = DBTests().connection()
        cur.fetchall.return_value.append(("required_extra", "text", "NO", None, "NO"))
        with self.assertRaisesRegex(loader.DryerDBError, "outside dryer contract"):
            loader.load_batch([final_row()], connection=conn)
        self.assertFalse(any(c.args[0].startswith("DELETE") for c in cur.execute.call_args_list))
        cur.executemany.assert_not_called()

    def test_setup_failure_reports_valid_requested_batch_and_table(self):
        for env_table, explicit, expected in ((TEST_TABLE, None, TEST_TABLE),
                                              ("invalid_table", TEST_TABLE, TEST_TABLE),
                                              ("invalid_table", None, None)):
            with self.subTest(env_table=env_table, explicit=explicit), scratch() as root:
                args = ["--run-root", str(root)] + (["--table", explicit] if explicit else [])
                try:
                    with patch.dict(sys.modules, {"lowes.step00_config": SimpleNamespace(load_env=lambda *a: None)}), \
                         patch.dict(os.environ, {"LOWES_DRYER_BATCH_ID": "l_setup_test", "LOWES_DRYER_TABLE": env_table,
                                                 "LOWES_DRYER_MAIN_LIMIT": "invalid_limit",
                                                 "LOWES_EMAIL_NOTIFY": "1", "LOWES_EMAIL_DRY_RUN": "0"}), \
                         patch.object(collector, "BrowserAPI") as browser, \
                         patch.object(mail, "_send_existing_lowes", return_value=True):
                        self.assertEqual(collector.main(args), 1)
                    browser.assert_not_called()
                    saved = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
                    notification = json.loads((root/"output/email_notify_manifest.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["batch_id"], "l_setup_test")
                    self.assertEqual(saved["table"], expected)
                    self.assertEqual(notification["metrics"]["table"], expected)
                    self.assertIn("l_setup_test", notification["body"])
                    self.assertIn("[Warning]", notification["subject"])
                finally:
                    logger = logging.getLogger("lowes.dryer")
                    for handler in logger.handlers[:]:
                        handler.close(); logger.removeHandler(handler)

    def test_invalid_setup_batch_uses_safe_generated_identifier(self):
        with scratch() as root:
            try:
                with patch.dict(sys.modules, {"lowes.step00_config": SimpleNamespace(load_env=lambda *a: None)}), \
                     patch.dict(os.environ, {"LOWES_DRYER_BATCH_ID": "../invalid_batch", "LOWES_DRYER_TABLE": TEST_TABLE,
                                             "LOWES_EMAIL_NOTIFY": "0"}):
                    self.assertEqual(collector.main(["--run-root", str(root)]), 1)
                saved = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
                self.assertRegex(saved["batch_id"], r"^l_\d{6}_\d{6}$")
                self.assertEqual(saved["table"], TEST_TABLE)
                self.assertNotIn("../invalid_batch", (root/"manifest.json").read_text(encoding="utf-8"))
            finally:
                logger = logging.getLogger("lowes.dryer")
                for handler in logger.handlers[:]:
                    handler.close(); logger.removeHandler(handler)

    def run_main(self, root, outcome="complete", table=None, sender_error=False,
                 collect_only=False, no_targets=False, notify_error=False, table_env=PRODUCTION_TABLE):
        summary = {"collection_status": outcome, "counts": {"rows": 1, "main": 1, "bsr": 1, "shared": 1},
                   "api_failure_products": int(outcome == "partial_failure"), "quality_warnings": 0}
        collection = MagicMock(return_value=([final_row()], summary))
        if no_targets:
            summary["counts"] = dict.fromkeys(("rows", "main", "bsr", "shared"), 0)
            collection.return_value = ([], summary)
        if outcome == "collection_failed": collection.side_effect = collector.CollectionError("synthetic_collection_failure")
        if outcome == "interrupted": collection.side_effect = KeyboardInterrupt()
        load = MagicMock(side_effect=lambda rows, **kw: {"table": kw["table"], "inserted": 1, "verified_count": 1})
        if outcome == "db_failed":
            summary["collection_status"] = "complete"
            load.side_effect = loader.DryerDBError("synthetic_db_failure")
        sender = MagicMock(side_effect=RuntimeError("PRIVATE_SMTP_PAYLOAD")) if sender_error else MagicMock(return_value=True)
        args = ["--run-root", str(root)] + (["--table", table] if table else [])
        if collect_only: args.append("--collect-only")
        try:
            with patch.dict(sys.modules, {"lowes.step00_config": SimpleNamespace(load_env=lambda *a: None)}), \
                 patch.dict(os.environ, {"LOWES_DRYER_BATCH_ID": "l_test", "LOWES_EMAIL_NOTIFY": "1", "LOWES_EMAIL_DRY_RUN": "0", "LOWES_DRYER_TABLE": table_env}), \
                 patch.object(collector.Settings, "from_env", side_effect=lambda env: Settings(target_table=env.get("LOWES_DRYER_TABLE", PRODUCTION_TABLE))), \
                 patch.object(collector, "BrowserAPI"), patch.object(collector, "legacy_operations", return_value={}), \
                 patch.object(collector, "run_collection", collection), patch.object(loader, "load_batch", load), \
                 patch.object(collector, "notify_run", wraps=mail.notify_run,
                              side_effect=OSError("synthetic_mail_write_error") if notify_error else None), \
                 patch.object(mail, "_send_existing_lowes", sender), redirect_stdout(io.StringIO()):
                code = collector.main(args)
        finally:
            logger = logging.getLogger("lowes.dryer")
            for handler in logger.handlers[:]:
                handler.close(); logger.removeHandler(handler)
        return code, json.loads((root/"manifest.json").read_text(encoding="utf-8")), load, sender

    def test_main_success_partial_failures_and_interrupt_notify_once_after_result(self):
        for outcome, expected in (("complete", 0), ("partial_failure", 2), ("collection_failed", 1), ("db_failed", 1), ("interrupted", 130)):
            with self.subTest(outcome=outcome), scratch() as root:
                code, saved, load, sender = self.run_main(root, outcome)
                self.assertEqual(code, expected)
                sender.assert_called_once()
                n = json.loads((root/"output/email_notify_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(n["collection_status"], saved["status"])
                if outcome not in ("complete",): self.assertIn("[Warning]", n["subject"])
                if outcome in ("complete", "partial_failure"):
                    self.assertTrue(saved["db_loaded"])
                    self.assertEqual(load.call_args.kwargs["table"], PRODUCTION_TABLE)

    def test_main_explicit_test_table_and_mail_failure_preserve_db_success(self):
        with scratch() as root:
            code, saved, load, sender = self.run_main(root, table=TEST_TABLE, sender_error=True)
            self.assertEqual(code, 0)
            self.assertTrue(saved["db_loaded"])
            self.assertEqual(saved["table"], TEST_TABLE)
            self.assertEqual(load.call_args.kwargs["table"], TEST_TABLE)
            self.assertEqual(saved["notification"]["status"], "delivery_unknown")
            self.assertNotIn("PRIVATE_SMTP_PAYLOAD", (root/"logs/dryer.log").read_text(encoding="utf-8"))

    def test_explicit_table_precedes_even_invalid_environment_table(self):
        with scratch() as root:
            code, saved, load, sender = self.run_main(root, table=TEST_TABLE, table_env="public.ldy_retail_com")
            self.assertEqual(code, 0)
            self.assertEqual(saved["table"], TEST_TABLE)
            self.assertEqual(load.call_args.kwargs["table"], TEST_TABLE)

    def test_collect_only_zero_targets_and_notification_write_failure(self):
        for collect_only, no_targets in ((True, False), (False, True)):
            with scratch() as root:
                code, saved, load, sender = self.run_main(root, collect_only=collect_only, no_targets=no_targets)
                self.assertEqual(code, 0)
                self.assertFalse(saved["db_loaded"])
                load.assert_not_called()
                n = json.loads((root/"output/email_notify_manifest.json").read_text(encoding="utf-8"))
                if collect_only:
                    self.assertIn("[Collect only]", n["subject"])
                else:
                    self.assertEqual(n["metrics"]["inserted"], 0)
        with scratch() as root:
            code, saved, load, sender = self.run_main(root, notify_error=True)
            self.assertEqual(code, 0)
            self.assertEqual(saved["status"], "complete")
            self.assertTrue(saved["db_loaded"])
            sender.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows BAT routing")
class ChainTests(unittest.TestCase):
    expected = ["BBY:REF", "BBY:LDY", "BBY:TV", "BBY:DRYER", "LOWES:REF", "LOWES:LDY", "LOWES:DRYER"]

    def bat(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))

    def chain(self, root, dryer_code=0, earlier_code=0, dry_run=False, missing=False):
        shutil.copyfile(REPO/"bby3_lowes2.bat", root/"bby3_lowes2.bat")
        # Inert clock avoids launching PowerShell; every category is a local stub.
        self.bat(root/"powershell.bat", "@echo off\necho 1000\n")
        self.bat(root/"bestbuy/new/_bby_daily_task.bat", '''@echo off
setlocal
echo BBY:%~1>>"%MOCK_TRACE%"
if "%~1"=="REF" exit /b %MOCK_EARLIER_EXIT%
cd /d "%~dp0"
exit /b 0
''')
        self.bat(root/"bestbuy/new/bby_dryer_daily_task.bat", '''@echo off
setlocal
echo BBY:DRYER>>"%MOCK_TRACE%"
cd /d "%~dp0"
exit /b 0
''')
        self.bat(root/"lowes/_lowes_daily_task.bat", '''@echo off
setlocal
if defined LOWES_DETAIL_LIMIT echo ENV_LEAK>>"%MOCK_TRACE%"
echo LOWES:%~1>>"%MOCK_TRACE%"
cd /d "%~dp0"
exit /b 0
''')
        if not missing:
            self.bat(root/"lowes/lowes_dryer_daily_task.bat", '''@echo off
setlocal
if defined LOWES_DETAIL_LIMIT echo ENV_LEAK>>"%MOCK_TRACE%"
if defined BESTBUY_DETAIL_LIMIT echo ENV_LEAK>>"%MOCK_TRACE%"
if not "%~1"=="" echo ARGUMENT_LEAK>>"%MOCK_TRACE%"
if /i not "%CD%"=="%MOCK_ROOT%" echo DIRECTORY_LEAK>>"%MOCK_TRACE%"
echo LOWES:DRYER>>"%MOCK_TRACE%"
cd /d "%~dp0"
exit /b %MOCK_DRYER_EXIT%
''')
        env = dict(os.environ, MOCK_TRACE=str(root/"trace.txt"), MOCK_ROOT=str(root),
                   MOCK_DRYER_EXIT=str(dryer_code), MOCK_EARLIER_EXIT=str(earlier_code),
                   LOWES_DAILY_TASK_BAT=str(root/"lowes/_lowes_daily_task.bat"),
                   BBY3_LOWES2_DRY_RUN="1" if dry_run else "0", LOWES_DETAIL_LIMIT="99", BESTBUY_DETAIL_LIMIT="99")
        result = subprocess.run(["cmd.exe", "/d", "/c", str(root/"bby3_lowes2.bat")], cwd=root.parent, env=env,
                                capture_output=True, text=True, errors="replace", timeout=30)
        trace = (root/"trace.txt").read_text().splitlines() if (root/"trace.txt").exists() else []
        logs = list((root/"task_logs").glob("bby3_lowes2_*.log"))
        self.assertEqual(len(logs), 1, result.stdout+result.stderr)
        return result, trace, logs[0].read_text()

    def test_seven_routes_last_dryer_directory_and_existing_failure_policy(self):
        for earlier, dryer, expected in ((0, 0, 0), (19, 0, 19), (0, 2, 2), (19, 17, 17)):
            with self.subTest(earlier=earlier, dryer=dryer), scratch() as tmp:
                root = tmp/"repo with spaces"; root.mkdir()
                result, trace, log = self.chain(root, dryer, earlier)
                self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
                self.assertEqual(trace, self.expected)
                self.assertIn("Lowes DRYER daily task exit_code="+str(dryer), log)

    def test_interrupt_and_missing_dryer_are_reported(self):
        for code in (130, -1073741510, 3221225786):
            with self.subTest(code=code), scratch() as root:
                result, trace, log = self.chain(root, dryer_code=code)
                self.assertEqual(result.returncode, 130, result.stdout+result.stderr)
                self.assertEqual(trace, self.expected)
        with scratch() as root:
            result, trace, log = self.chain(root, missing=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(trace, self.expected[:-1])
            self.assertIn("LOWES_DRYER(9002)", log)
        with scratch() as root:
            result, trace, log = self.chain(root, earlier_code=130)
            self.assertEqual(result.returncode, 130)
            self.assertEqual(trace, self.expected[:1])

    def test_dry_run_lists_seven_without_running_any_category(self):
        with scratch() as root:
            result, trace, log = self.chain(root, dry_run=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertEqual(trace, [])
            planned = [line for line in log.splitlines() if line.startswith("[dry-run] call")]
            self.assertEqual(len(planned), 7)
            self.assertIn("lowes_dryer_daily_task.bat", planned[-1])

    def test_standalone_bat_defaults_exit_code_and_restores_directory(self):
        for code, override in ((0, ""), (2, "1"), (1, ""), (130, "")):
            with self.subTest(code=code), scratch() as tmp:
                root = tmp/"repo with spaces"; (root/"lowes").mkdir(parents=True)
                shutil.copyfile(REPO/"lowes/lowes_dryer_daily_task.bat", root/"lowes/lowes_dryer_daily_task.bat")
                (root/"lowes/__init__.py").touch()
                (root/"lowes/lowes_dryer.py").write_text('''import json,os,sys
from pathlib import Path
Path(os.environ['MOCK_TRACE']).write_text(json.dumps({k:os.getenv(k) for k in ('LOWES_DRYER_MAIN_LIMIT','LOWES_DRYER_BSR_LIMIT','LOWES_DRYER_DETAIL_CONCURRENCY','LOWES_DRYER_TABLE')}))
raise SystemExit(int(os.environ['MOCK_DRYER_EXIT']))
''')
                wrapper = tmp/"wrapper.bat"
                self.bat(wrapper, f'''@echo off
call "{root / 'lowes/lowes_dryer_daily_task.bat'}"
set "GOT_EXIT=%ERRORLEVEL%"
if /i not "%CD%"=="%MOCK_PARENT%" exit /b 88
if not "%LOWES_DRYER_DETAIL_CONCURRENCY%"=="%MOCK_OVERRIDE%" exit /b 89
exit /b %GOT_EXIT%
''')
                env = dict(os.environ, MOCK_TRACE=str(tmp/"trace.json"), MOCK_DRYER_EXIT=str(code),
                           MOCK_PARENT=str(tmp), MOCK_OVERRIDE=override,
                           PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ.get("PATH", ""))
                for k in ("LOWES_DRYER_MAIN_LIMIT", "LOWES_DRYER_BSR_LIMIT", "LOWES_DRYER_DETAIL_CONCURRENCY", "LOWES_DRYER_TABLE"):
                    env.pop(k, None)
                if override: env["LOWES_DRYER_DETAIL_CONCURRENCY"] = override
                result = subprocess.run(["cmd.exe", "/d", "/c", str(wrapper)], cwd=tmp, env=env,
                                        capture_output=True, text=True, errors="replace", timeout=30)
                self.assertEqual(result.returncode, code, result.stdout+result.stderr)
                trace = json.loads((tmp/"trace.json").read_text())
                self.assertEqual((trace['LOWES_DRYER_MAIN_LIMIT'], trace['LOWES_DRYER_BSR_LIMIT']), ('300', '100'))
                self.assertEqual(trace['LOWES_DRYER_DETAIL_CONCURRENCY'], override or '2')


if __name__ == "__main__":
    unittest.main()
