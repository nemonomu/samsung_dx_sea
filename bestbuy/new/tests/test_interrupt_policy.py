import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bestbuy import bestbuy_orchestrator


REPO_ROOT = Path(__file__).resolve().parents[3]


class InterruptPolicyTests(unittest.TestCase):
    def test_bestbuy_orchestrator_recognizes_windows_and_conventional_interrupt_codes(self):
        for code in (130, -1073741510, 3221225786, "130"):
            with self.subTest(code=code):
                self.assertTrue(bestbuy_orchestrator.is_interrupt_exit_code(code))
        for code in (0, 1, None, "invalid"):
            with self.subTest(code=code):
                self.assertFalse(bestbuy_orchestrator.is_interrupt_exit_code(code))

    def test_fullrun_routes_interrupt_around_failure_notification(self):
        source = (REPO_ROOT / "bestbuy" / "new" / "run_bestbuy_fullrun.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn('set "STEP_EXIT=%ERRORLEVEL%"', source)
        self.assertIn('if not "%STEP_EXIT%"=="0" (', source)
        self.assertIn('if "%RUN_INTERRUPTED%"=="1" goto :interrupted', source)
        interrupted = source.index("\n:interrupted\n")
        notify = source.index("\n:notify\n", interrupted)
        self.assertLess(interrupted, notify)
        self.assertNotIn("call :notify", source[interrupted:notify])

    def test_daily_wrappers_normalize_native_windows_ctrl_c(self):
        for relative_path in (
            Path("bestbuy/new/_bby_daily_task.bat"),
            Path("lowes/_lowes_daily_task.bat"),
        ):
            with self.subTest(path=relative_path):
                source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('if "%EXIT_CODE%"=="-1073741510" set "EXIT_CODE=130"', source)
                self.assertIn('if "%EXIT_CODE%"=="3221225786" set "EXIT_CODE=130"', source)

    def test_combined_chain_stops_interrupt_but_keeps_normal_failure_continuation(self):
        source = (REPO_ROOT / "bby3_lowes2.bat").read_text(encoding="utf-8")
        self.assertEqual(source.count('if "!CATEGORY_EXIT!"=="130" goto :interrupted'), 2)
        self.assertIn("call :record_failure BBY_%%C !CATEGORY_EXIT!", source)
        self.assertIn("call :record_failure LOWES_%%C !CATEGORY_EXIT!", source)
        self.assertIn("continue_on_category_failure=true", source)
        self.assertIn("exit /b 130", source)

    def test_lowes_orchestrator_has_same_interrupt_contract(self):
        source = (REPO_ROOT / "lowes" / "lowes_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("INTERRUPT_EXIT_CODE = 130", source)
        self.assertIn("if is_interrupt_exit_code(code):", source)
        self.assertIn("except KeyboardInterrupt:", source)

    def test_fullrun_forces_safe_preflight_and_never_inherits_canary_only(self):
        for step_key in ("08", "09"):
            with self.subTest(step=step_key):
                step = bestbuy_orchestrator.step_by_key(step_key)
                self.assertEqual(step.env["BESTBUY_DETAIL_BROWSER_GRAPHQL_CANARY_ONLY"], "0")
                self.assertEqual(step.env["BESTBUY_DETAIL_BROWSER_GRAPHQL_PREFLIGHT_SIZE"], "1")
                if step.resume_env:
                    self.assertEqual(step.resume_env["BESTBUY_DETAIL_BROWSER_GRAPHQL_CANARY_ONLY"], "0")
                    self.assertEqual(step.resume_env["BESTBUY_DETAIL_BROWSER_GRAPHQL_PREFLIGHT_SIZE"], "1")

        unsafe_shell = {
            "BESTBUY_DETAIL_BROWSER_GRAPHQL_CANARY_ONLY": "1",
            "BESTBUY_DETAIL_BROWSER_GRAPHQL_PREFLIGHT_SIZE": "5",
            "BESTBUY_FORCE_STEP_ENV": "0",
        }
        with patch.dict(os.environ, unsafe_shell), patch.object(
            bestbuy_orchestrator.subprocess, "run"
        ) as run_mock:
            bestbuy_orchestrator.run_step(bestbuy_orchestrator.step_by_key("08"))
        child_env = run_mock.call_args.kwargs["env"]
        self.assertEqual(child_env["BESTBUY_DETAIL_BROWSER_GRAPHQL_CANARY_ONLY"], "0")
        self.assertEqual(child_env["BESTBUY_DETAIL_BROWSER_GRAPHQL_PREFLIGHT_SIZE"], "1")


if __name__ == "__main__":
    unittest.main()
