import os


DEFAULT_CHROME_VERSION_MAIN = "148"


def chrome_version_main():
    raw = os.getenv("LOWES_UC_VERSION_MAIN", DEFAULT_CHROME_VERSION_MAIN).strip()
    if not raw:
        return None
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
