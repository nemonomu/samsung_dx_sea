"""Execute real Windows BAT routing with isolated, inert category stubs."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
EXPECTED = ["BBY:REF", "BBY:LDY", "BBY:TV", "BBY:DRYER", "LOWES:REF", "LOWES:LDY"]


@unittest.skipUnless(os.name == "nt", "Windows BAT execution")
class DryerChainTests(unittest.TestCase):
    def write_bat(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.replace("\n", "\r\n").encode("ascii"))

    def chain(self, root, dryer_code=0, dry_run=False, bby_code=0):
        shutil.copyfile(REPO / "bby3_lowes2.bat", root / "bby3_lowes2.bat")
        self.write_bat(root / "bestbuy/new/_bby_daily_task.bat", """@echo off
setlocal
if defined BESTBUY_DETAIL_LIMIT echo UNCLEARED_BBY>>"%MOCK_TRACE%"
echo BBY:%~1>>"%MOCK_TRACE%"
set "BESTBUY_CATEGORY=changed_inside_child"
cd /d "%~dp0"
if "%~1"=="REF" exit /b %MOCK_BBY_EXIT%
exit /b 0
""")
        self.write_bat(root / "bestbuy/new/bby_dryer_daily_task.bat", """@echo off
setlocal
if not "%~1"=="" echo UNEXPECTED_DRYER_ARGUMENT>>"%MOCK_TRACE%"
if defined BESTBUY_DETAIL_LIMIT echo UNCLEARED_DRYER>>"%MOCK_TRACE%"
echo BBY:DRYER>>"%MOCK_TRACE%"
set "BESTBUY_CATEGORY=changed_inside_dryer"
cd /d "%~dp0"
exit /b %MOCK_DRYER_EXIT%
""")
        lowes = root / "lowes/_lowes_daily_task.bat"
        self.write_bat(lowes, """@echo off
setlocal
if defined BESTBUY_DETAIL_LIMIT echo UNCLEARED_BBY_FOR_LOWES>>"%MOCK_TRACE%"
if defined LOWES_DETAIL_LIMIT echo UNCLEARED_LOWES>>"%MOCK_TRACE%"
if "%BESTBUY_CATEGORY%"=="changed_inside_dryer" echo LEAKED_DRYER_ENV>>"%MOCK_TRACE%"
if /i not "%CD%"=="%MOCK_ROOT%" echo LEAKED_CHILD_DIRECTORY>>"%MOCK_TRACE%"
echo LOWES:%~1>>"%MOCK_TRACE%"
cd /d "%~dp0"
exit /b 0
""")
        env = dict(os.environ, LOWES_DAILY_TASK_BAT=str(lowes), MOCK_TRACE=str(root / "trace.txt"),
                   MOCK_ROOT=str(root), MOCK_DRYER_EXIT=str(dryer_code), MOCK_BBY_EXIT=str(bby_code),
                   BBY3_LOWES2_DRY_RUN="1" if dry_run else "0", BESTBUY_DETAIL_LIMIT="99", LOWES_DETAIL_LIMIT="99")
        result = subprocess.run(["cmd.exe", "/d", "/c", "bby3_lowes2.bat"], cwd=root, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        trace = (root / "trace.txt").read_text().splitlines() if (root / "trace.txt").exists() else []
        logs = list((root / "task_logs").glob("bby3_lowes2_*.log"))
        self.assertEqual(len(logs), 1, result.stdout + result.stderr)
        return result, trace, logs[0].read_text()

    def test_all_six_routes_preserve_existing_order_environment_and_directory(self):
        with tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
            result, trace, log = self.chain(Path(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(trace, EXPECTED)
        self.assertEqual(len(re.findall(r"daily task exit_code=0 elapsed_sec=\d+", log)), 6)
        self.assertRegex(log, r"\nelapsed_sec=\d+")

    def test_dryer_failure_continues_lowes_and_is_not_masked(self):
        with tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
            result, trace, log = self.chain(Path(directory), dryer_code=17)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(trace, EXPECTED)
        self.assertIn("BBY_DRYER(17)", log)
        self.assertIn("Lowes LDY daily task exit_code=0", log)

    def test_existing_bby_failure_still_continues_dryer_and_lowes(self):
        with tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
            result, trace, log = self.chain(Path(directory), bby_code=19)
        self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
        self.assertEqual(trace, EXPECTED)
        self.assertIn("BBY_REF(19)", log)

    def test_interrupt_codes_skip_remaining_products(self):
        for code in (130, -1073741510, 3221225786):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
                result, trace, log = self.chain(Path(directory), dryer_code=code)
            self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
            self.assertEqual(trace, EXPECTED[:4])
            self.assertIn("Remaining categories skipped", log)

    def test_dry_run_routes_six_without_executing_any_category(self):
        with tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
            result, trace, log = self.chain(Path(directory), dry_run=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(trace, [])
        planned = [line for line in log.splitlines() if line.startswith("[dry-run] call ")]
        self.assertEqual(len(planned), 6)
        self.assertIn('bby_dryer_daily_task.bat"', planned[3])
        self.assertNotIn('bby_dryer_daily_task.bat" DRYER', planned[3])

    def test_actual_standalone_bat_help_restores_callers_directory_and_environment(self):
        with tempfile.TemporaryDirectory(prefix="bby_dryer_chain_") as directory:
            root = Path(directory)
            bat = REPO / "bestbuy/new/bby_dryer_daily_task.bat"
            wrapper = root / "wrapper.bat"
            self.write_bat(wrapper, f'''@echo off
set "BESTBUY_CATEGORY=CALLER"
call "{bat}" --help
set "HELP_EXIT=%ERRORLEVEL%"
if /i not "%CD%"=="%MOCK_ROOT%" exit /b 88
if not "%BESTBUY_CATEGORY%"=="CALLER" exit /b 89
exit /b %HELP_EXIT%
''')
            env = dict(os.environ, BESTBUY_PYTHON=sys.executable, MOCK_ROOT=str(root))
            result = subprocess.run(["cmd.exe", "/d", "/c", str(wrapper)], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("public.ldy_dryer_retail only", result.stdout)


if __name__ == "__main__":
    unittest.main()
