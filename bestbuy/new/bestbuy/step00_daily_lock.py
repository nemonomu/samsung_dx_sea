import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def append_log(path, message):
    line = f"[{now_text()}] {message}"
    print(line, flush=True)
    if path:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")


def powershell_string(value):
    return json.dumps(str(value or ""))


def active_bestbuy_processes(category, root):
    category = str(category or "").strip()
    root = str(root or "").strip()
    current_pid = os.getpid()
    script = f"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$category = {powershell_string(category)}
$root = {powershell_string(root)}
$currentPid = {current_pid}
$processes = @()
try {{
    $processes = Get-CimInstance Win32_Process -ErrorAction Stop
}} catch {{
    $processes = Get-WmiObject Win32_Process -ErrorAction Stop
}}
$matches = $processes | Where-Object {{
    $_.ProcessId -ne $currentPid -and
    $_.CommandLine -and
    (
        (($_.CommandLine -match 'run_bestbuy_fullrun\\.bat') -and ($_.CommandLine -match [regex]::Escape($category))) -or
        (($_.CommandLine -match 'bestbuy\\.bestbuy_orchestrator') -and ($_.CommandLine -match [regex]::Escape($category))) -or
        (($_.CommandLine -match 'step08_availability_backfill') -and ($root -and $_.CommandLine -match [regex]::Escape($root)))
    )
}} | Select-Object -First 10 ProcessId,Name,CommandLine
$matches | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


def any_python_process():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    text = result.stdout.lower()
    return "python.exe" in text


def process_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() == str(pid)
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) > 1 and parts[1] == str(pid):
            return True
    return False


def read_lock_payload(lock_path):
    try:
        return json.loads(Path(lock_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lock_owner_active(lock_path):
    payload = read_lock_payload(lock_path)
    parent_pid = payload.get("parent_pid")
    if parent_pid:
        return process_exists(parent_pid)
    return None


def lock_age_hours(lock_path):
    try:
        modified = datetime.fromtimestamp(Path(lock_path).stat().st_mtime)
    except OSError:
        return None
    return max(0.0, (datetime.now() - modified).total_seconds() / 3600.0)


def acquire(args):
    lock_path = Path(args.lock)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    active = []
    if lock_path.exists():
        active = active_bestbuy_processes(args.category, args.root)
        if active is None:
            owner_active = lock_owner_active(lock_path)
            if owner_active is True:
                append_log(
                    args.log,
                    f"Cannot inspect process command lines and lock owner process is still active; keeping lock_file={lock_path}",
                )
                return 2
            if owner_active is False:
                append_log(
                    args.log,
                    f"Cannot inspect process command lines but lock owner process ended; treating lock as stale lock_file={lock_path}",
                )
                active = []
            elif any_python_process():
                append_log(
                    args.log,
                    f"Cannot inspect process command lines and python.exe is running; keeping lock_file={lock_path}",
                )
                return 2
            else:
                append_log(
                    args.log,
                    f"Cannot inspect process command lines but no python.exe is running; treating lock as stale lock_file={lock_path}",
                )
                active = []
        if active:
            pids = ",".join(str(proc.get("ProcessId", "")) for proc in active if proc.get("ProcessId"))
            append_log(
                args.log,
                f"Previous BestBuy {args.category} task is still active; keeping lock_file={lock_path} active_pids={pids}",
            )
            return 2
        age = lock_age_hours(lock_path)
        age_text = "" if age is None else f" age_hours={age:.2f}"
        append_log(
            args.log,
            f"Removing stale BestBuy {args.category} lock_file={lock_path}{age_text}",
        )
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    payload = {
        "category": args.category,
        "created_at": now_text(),
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "root": str(args.root or ""),
    }
    try:
        with lock_path.open("x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.write("\n")
    except FileExistsError:
        append_log(args.log, f"BestBuy {args.category} lock appeared while acquiring lock_file={lock_path}")
        return 2
    append_log(args.log, f"Acquired BestBuy {args.category} lock_file={lock_path}")
    return 0


def release(args):
    lock_path = Path(args.lock)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return 0
    append_log(args.log, f"Released BestBuy {args.category} lock_file={lock_path}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["acquire", "release"])
    parser.add_argument("--lock", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args()
    if args.action == "acquire":
        return acquire(args)
    return release(args)


if __name__ == "__main__":
    sys.exit(main())
