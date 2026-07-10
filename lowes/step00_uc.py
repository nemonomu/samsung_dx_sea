import os
import re
import subprocess
from pathlib import Path


DEFAULT_CHROME_VERSION_MAIN = "auto"


def chrome_exe_candidates():
    candidates = []
    env_path = os.getenv("LOWES_CHROME_EXE", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.getenv(env_name, "").strip()
        if not base:
            continue
        candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return candidates


def detect_chrome_version_main():
    for chrome_path in chrome_exe_candidates():
        if not chrome_path.exists():
            continue
        try:
            result = subprocess.run(
                [str(chrome_path), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            continue
        version_text = f"{result.stdout} {result.stderr}"
        match = re.search(r"(\d+)\.", version_text)
        if match:
            return int(match.group(1))
    return None


def chrome_version_main():
    raw = os.getenv("LOWES_UC_VERSION_MAIN", DEFAULT_CHROME_VERSION_MAIN).strip().lower()
    if raw in {"", "0", "none", "false", "off"}:
        return None
    if raw == "auto":
        return detect_chrome_version_main()
    try:
        return int(float(raw))
    except ValueError:
        return None


def launch_chrome(uc_module, options, headless=False, **kwargs):
    version_main = chrome_version_main()
    if version_main:
        kwargs.setdefault("version_main", version_main)
    browser_executable_path = os.getenv("LOWES_CHROME_EXE", "").strip()
    if browser_executable_path:
        kwargs.setdefault("browser_executable_path", browser_executable_path)
    kwargs.setdefault("use_subprocess", True)
    return uc_module.Chrome(options=options, headless=headless, **kwargs)
