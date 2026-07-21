import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lowes import step00_uc


class ChromeVersionDetectionTests(unittest.TestCase):
    def test_parse_chrome_version_main(self):
        self.assertEqual(step00_uc.parse_chrome_version_main("150.0.7871.125"), 150)
        self.assertEqual(step00_uc.parse_chrome_version_main("Google Chrome 151.2"), 151)
        self.assertIsNone(step00_uc.parse_chrome_version_main("unknown"))

    @patch("lowes.step00_uc.subprocess.run")
    def test_windows_file_version_uses_powershell_metadata(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="150.0.7871.125\n", stderr="")

        with patch("lowes.step00_uc.os.name", "nt"):
            version_main = step00_uc.chrome_executable_version_main(
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
            )

        self.assertEqual(version_main, 150)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("VersionInfo.ProductVersion", command[-1])

    @patch("lowes.step00_uc.detect_chrome_installation")
    def test_explicit_version_bypasses_auto_detection(self, detect):
        with patch.dict(os.environ, {"LOWES_UC_VERSION_MAIN": "150"}, clear=False):
            os.environ.pop("LOWES_CHROME_EXE", None)
            version_main, chrome_path, source = step00_uc.chrome_launch_settings()

        self.assertEqual(version_main, 150)
        self.assertIsNone(chrome_path)
        self.assertEqual(source, "configured")
        detect.assert_not_called()

    @patch("lowes.step00_uc.detect_chrome_installation")
    def test_auto_uses_detected_version_and_executable(self, detect):
        chrome_path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        detect.return_value = (chrome_path, 150)

        with patch.dict(os.environ, {"LOWES_UC_VERSION_MAIN": "auto"}, clear=False):
            os.environ.pop("LOWES_CHROME_EXE", None)
            settings = step00_uc.chrome_launch_settings()

        self.assertEqual(settings, (150, chrome_path, "auto"))

    @patch("lowes.step00_uc.chrome_exe_candidates", return_value=[])
    @patch("lowes.step00_uc.detect_chrome_installation", return_value=(None, None))
    def test_auto_detection_failure_is_actionable(self, _detect, _candidates):
        with patch.dict(os.environ, {"LOWES_UC_VERSION_MAIN": "auto"}, clear=False):
            os.environ.pop("LOWES_CHROME_EXE", None)
            with self.assertRaisesRegex(RuntimeError, "could not detect"):
                step00_uc.chrome_launch_settings()

    @patch("lowes.step00_uc.chrome_launch_settings")
    def test_launch_chrome_passes_detected_version_and_path(self, settings):
        chrome_path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        settings.return_value = (150, chrome_path, "auto")
        uc_module = SimpleNamespace(Chrome=Mock(return_value="driver"))

        driver = step00_uc.launch_chrome(uc_module, options="options", headless=False)

        self.assertEqual(driver, "driver")
        uc_module.Chrome.assert_called_once_with(
            options="options",
            headless=False,
            version_main=150,
            browser_executable_path=str(chrome_path),
            use_subprocess=True,
        )


if __name__ == "__main__":
    unittest.main()
