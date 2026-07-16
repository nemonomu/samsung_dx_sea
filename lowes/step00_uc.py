import os
import re
import subprocess
from pathlib import Path


DEFAULT_CHROME_VERSION_MAIN = "auto"
CHROME_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)(?:\.\d+){1,3}(?!\d)")


def chrome_exe_candidates():
    env_path = os.getenv("LOWES_CHROME_EXE", "").strip()
    if env_path:
        return [Path(env_path)]
    candidates = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.getenv(env_name, "").strip()
        if not base:
            continue
        candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return candidates


def parse_chrome_version_main(version_text):
    match = CHROME_VERSION_PATTERN.search(str(version_text or ""))
    return int(match.group(1)) if match else None


def chrome_executable_version_main(chrome_path):
    """Read the installed Chrome executable's major version without launching it."""
    chrome_path = Path(chrome_path)
    try:
        if os.name == "nt":
            escaped_path = str(chrome_path).replace("'", "''")
            command = f"(Get-Item -LiteralPath '{escaped_path}').VersionInfo.ProductVersion"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            result = subprocess.run(
                [str(chrome_path), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_chrome_version_main(f"{result.stdout} {result.stderr}")


def detect_chrome_installation():
    for chrome_path in chrome_exe_candidates():
        if not chrome_path.is_file():
            continue
        version_main = chrome_executable_version_main(chrome_path)
        if version_main:
            return chrome_path, version_main
    return None, None


def detect_chrome_version_main():
    _, version_main = detect_chrome_installation()
    return version_main


def chrome_launch_settings():
    raw = os.getenv("LOWES_UC_VERSION_MAIN", DEFAULT_CHROME_VERSION_MAIN).strip().lower()
    configured_executable = os.getenv("LOWES_CHROME_EXE", "").strip()
    if raw in {"", "0", "none", "false", "off"}:
        return None, Path(configured_executable) if configured_executable else None, "disabled"
    if raw == "auto":
        chrome_path, version_main = detect_chrome_installation()
        if not version_main:
            checked_paths = ", ".join(str(path) for path in chrome_exe_candidates()) or "(none)"
            raise RuntimeError(
                "LOWES_UC_VERSION_MAIN=auto could not detect the installed Chrome version. "
                f"Checked: {checked_paths}. Set LOWES_CHROME_EXE to the active chrome.exe path "
                "or set LOWES_UC_VERSION_MAIN to the installed Chrome major version."
            )
        return version_main, chrome_path, "auto"
    try:
        version_main = int(float(raw))
    except ValueError as exc:
        raise ValueError(f"Invalid LOWES_UC_VERSION_MAIN value: {raw!r}") from exc
    return version_main, Path(configured_executable) if configured_executable else None, "configured"


def chrome_version_main():
    version_main, _, _ = chrome_launch_settings()
    return version_main


def launch_chrome(uc_module, options, headless=False, **kwargs):
    version_main, chrome_path, version_source = chrome_launch_settings()
    if version_main:
        kwargs.setdefault("version_main", version_main)
    if chrome_path:
        kwargs.setdefault("browser_executable_path", str(chrome_path))
    kwargs.setdefault("use_subprocess", True)
    print(
        f"[lowes.uc] version_main={version_main or '-'} source={version_source} "
        f"chrome_exe={chrome_path or 'uc-default'}"
    )
    return uc_module.Chrome(options=options, headless=headless, **kwargs)
