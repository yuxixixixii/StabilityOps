#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/vllm_models.json}"
RUN_DIR="${RUN_DIR:-runs/vllm}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-10}"

python3 - "$CONFIG" "$RUN_DIR" "$TIMEOUT_SECONDS" "$POLL_SECONDS" <<'PY'
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

config = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
timeout_seconds = int(float(sys.argv[3]))
poll_seconds = float(sys.argv[4])
services = json.loads(config.read_text(encoding="utf-8"))["services"]
deadline = time.time() + timeout_seconds
ready = set()

while time.time() < deadline:
    for service in services:
        alias = service["alias"]
        if alias in ready:
            continue
        pid_path = run_dir / f"{alias}.pid"
        pid = pid_path.read_text().strip() if pid_path.exists() else ""
        if pid:
            proc = subprocess.run(["ps", "-p", pid, "-o", "stat="], text=True, stdout=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                raise SystemExit(f"{alias}: process not running pid={pid}")
        url = f"http://{service.get('host', '127.0.0.1')}:{service['port']}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                response.read()
            ready.add(alias)
            print(f"{alias}: ready port={service['port']}", flush=True)
        except Exception as exc:
            print(f"{alias}: waiting port={service['port']} reason={exc!r}", flush=True)
    if len(ready) == len(services):
        raise SystemExit(0)
    time.sleep(poll_seconds)

missing = [service["alias"] for service in services if service["alias"] not in ready]
for service in services:
    log_path = run_dir / f"{service['alias']}.log"
    if service["alias"] in missing and log_path.exists():
        print(f"--- tail {log_path}", flush=True)
        print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]), flush=True)
raise SystemExit(f"timed out waiting for vLLM services: {', '.join(missing)}")
PY
