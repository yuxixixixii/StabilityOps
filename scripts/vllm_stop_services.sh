#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/vllm_models.json}"
RUN_DIR="${RUN_DIR:-runs/vllm}"

python3 - "$CONFIG" "$RUN_DIR" <<'PY'
import json
import os
import signal
import sys
import time
from pathlib import Path


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_exit(pid: int, timeout_seconds: float = 30.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.5)
    return not process_exists(pid)

config = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
data = json.loads(config.read_text(encoding="utf-8"))
for service in data["services"]:
    alias = service["alias"]
    pgid_path = run_dir / f"{alias}.pgid"
    pid_path = run_dir / f"{alias}.pid"
    pgid = pgid_path.read_text().strip() if pgid_path.exists() else ""
    pid = pid_path.read_text().strip() if pid_path.exists() else ""
    stopped = False
    try:
        if pgid:
            os.killpg(int(pgid), signal.SIGTERM)
            print(f"{alias}: sent SIGTERM to pgid={pgid}")
        elif pid:
            os.kill(int(pid), signal.SIGTERM)
            print(f"{alias}: sent SIGTERM to pid={pid}")
        else:
            print(f"{alias}: no pid/pgid file")
    except ProcessLookupError:
        print(f"{alias}: not running")
        stopped = True
    except PermissionError as exc:
        print(f"{alias}: permission error {exc!r}")

    if pid:
        try:
            stopped = wait_for_exit(int(pid), timeout_seconds=30.0)
        except ValueError:
            stopped = True
    if not stopped and pgid:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
            print(f"{alias}: sent SIGKILL to pgid={pgid}")
            if pid:
                stopped = wait_for_exit(int(pid), timeout_seconds=10.0)
        except ProcessLookupError:
            stopped = True
    if stopped:
        pid_path.unlink(missing_ok=True)
        pgid_path.unlink(missing_ok=True)
        print(f"{alias}: stopped")
    else:
        print(f"{alias}: stop requested but process still exists pid={pid}")
PY
