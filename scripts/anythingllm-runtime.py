#!/usr/bin/env python3
"""Manage this checkout's private source runtime without touching the course API."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
from openclass_core.services.anythingllm.config import AnythingLLMConfig, ROOT
from openclass_core.services.anythingllm.runtime import ensure_started, healthy


def stop(config):
    manifest = config.storage / "runtime-pids.json"
    if not manifest.is_file(): return
    for process in json.loads(manifest.read_text()):
        if process.get("script") not in {"server.cjs", "collector.cjs"}: continue
        command = subprocess.run(["ps", "-p", str(process["pid"]), "-o", "command="], capture_output=True, text=True).stdout
        if str(ROOT / "integrations/anythingllm" / process["script"]) in command:
            try: os.kill(process["pid"], signal.SIGTERM)
            except ProcessLookupError: pass
    time.sleep(.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "foreground"])
    args = parser.parse_args(); config = AnythingLLMConfig.from_env()
    if args.command in {"stop", "restart"}: stop(config)
    if args.command in {"start", "restart", "foreground"}: ensure_started(config)
    if args.command == "foreground":
        try:
            while True:
                records = json.loads((config.storage / "runtime-pids.json").read_text())
                for script in ("server.cjs", "collector.cjs"):
                    candidates = [item for item in records if item.get("script") == script]
                    alive = False
                    for item in candidates:
                        result = subprocess.run(["ps", "-p", str(item["pid"]), "-o", "stat=,command="], capture_output=True, text=True)
                        line = result.stdout.strip()
                        if line and not line.startswith("Z") and str(ROOT / "integrations/anythingllm" / script) in line:
                            alive = True
                    if not alive:
                        raise RuntimeError("A supervised AnythingLLM child process exited.")
                time.sleep(2)
        except KeyboardInterrupt: pass
        finally: stop(config)
    print(json.dumps({"server": healthy(config), "collector": healthy(config, collector=True), "local_ocr": False}))


if __name__ == "__main__": main()
