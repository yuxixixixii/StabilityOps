#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/vllm_models.json}"
VLLM_PYTHON="${VLLM_PYTHON:-$(pwd)/.venv-vllm/bin/python}"
RUN_DIR="${RUN_DIR:-runs/vllm}"

mkdir -p "$RUN_DIR"

"$VLLM_PYTHON" - "$CONFIG" "$RUN_DIR" <<'PY'
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

config = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
data = json.loads(config.read_text(encoding="utf-8"))

for service in data["services"]:
    alias = service["alias"]
    pid_path = run_dir / f"{alias}.pid"
    pgid_path = run_dir / f"{alias}.pgid"
    log_path = run_dir / f"{alias}.log"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            print(f"{alias}: already running pid={pid}")
            continue
        except Exception:
            pass

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(service["cuda_visible_devices"])
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    env.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    cmd = [
        os.environ.get("VLLM_PYTHON", sys.executable),
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        service["model"],
        "--served-model-name",
        service.get("served_model_name", service["model"]),
        "--host",
        service.get("host", "127.0.0.1"),
        "--port",
        str(service["port"]),
        "--gpu-memory-utilization",
        str(service.get("gpu_memory_utilization", 0.90)),
        "--max-model-len",
        str(service.get("max_model_len", 32768)),
        "--trust-remote-code",
    ]
    for extra_arg in service.get("extra_args", []):
        cmd.append(str(extra_arg))
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            start_new_session=True,
        )
    pgid = os.getpgid(proc.pid)
    pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
    pgid_path.write_text(str(pgid) + "\n", encoding="utf-8")
    print(f"{alias}: started pid={proc.pid} pgid={pgid} gpu={service['cuda_visible_devices']} port={service['port']} log={log_path}")
    print("  " + " ".join(shlex.quote(part) for part in cmd))
PY
