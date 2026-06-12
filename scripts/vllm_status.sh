#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/vllm_models.json}"
RUN_DIR="${RUN_DIR:-runs/vllm}"

echo "== GPU =="
nvidia-smi --query-gpu=index,name,memory.used,memory.free,utilization.gpu --format=csv,noheader || true

echo
echo "== services =="
python3 - "$CONFIG" "$RUN_DIR" <<'PY'
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

config = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
data = json.loads(config.read_text(encoding="utf-8"))
for service in data["services"]:
    alias = service["alias"]
    pid_path = run_dir / f"{alias}.pid"
    pid = pid_path.read_text().strip() if pid_path.exists() else ""
    print(f"-- {alias} port={service['port']} pid={pid or 'missing'}")
    if pid:
        subprocess.run(["ps", "-p", pid, "-o", "pid,pgid,stat,etime,%cpu,%mem,rss,cmd"], check=False)
    url = f"http://{service.get('host', '127.0.0.1')}:{service['port']}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            print(response.read().decode("utf-8")[:500])
    except Exception as exc:
        print(f"not ready: {exc!r}")
PY

echo
echo "== recent logs =="
find "$RUN_DIR" -maxdepth 1 -name '*.log' -type f -print 2>/dev/null | while read -r log; do
  echo "--- $log"
  tail -n 20 "$log" || true
done
