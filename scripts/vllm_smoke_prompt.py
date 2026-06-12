#!/usr/bin/env python3
"""Send a short OpenAI-compatible chat request to each configured vLLM endpoint."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=Path("configs/vllm_models.json"), type=Path)
    parser.add_argument("--timeout", default=60, type=int)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    for service in config["services"]:
        url = f"http://{service.get('host', '127.0.0.1')}:{service['port']}/v1/chat/completions"
        payload = {
            "model": service["model"],
            "messages": [{"role": "user", "content": "Return JSON only: {\"ok\": true}"}],
            "temperature": 0,
            "max_tokens": 32,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
            method="POST",
        )
        print(f"== {service['alias']} {url}")
        with urllib.request.urlopen(req, timeout=args.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        print(json.dumps(body, ensure_ascii=False)[:1000])


if __name__ == "__main__":
    main()
