#!/usr/bin/env python3
"""
karya CLI
Usage:
  karya start              # start the autonomous loop
  karya run-once           # run a single cycle and exit
  karya doctor             # check system, model, ollama
  karya status             # show current world state
  karya bench              # quick TPS benchmark
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path


def cmd_start(args):
    from karya.core.loop import AgentLoop
    goals, config = _load_goals(args.goals)
    backend = getattr(args, "backend", None) or config.get("backend", "ollama")
    base_url = getattr(args, "base_url", "") or config.get("base_url",
        "http://localhost:8080" if backend == "llamacpp" else "http://localhost:11434")
    loop = AgentLoop(
        goals=goals,
        model=config.get("model") or None,
        base_url=base_url,
        backend=backend,
        dry_run=args.dry_run or config.get("dry_run", False),
        safe_gpio_pins=config.get("safe_gpio_pins", []),
        thresholds=config.get("thresholds", []),
        hil_config=config.get("hil", {}),
    )
    loop.run_forever()


def cmd_run_once(args):
    from karya.core.loop import AgentLoop
    goals, config = _load_goals(args.goals)
    backend = getattr(args, "backend", None) or config.get("backend", "ollama")
    base_url = getattr(args, "base_url", "") or config.get("base_url",
        "http://localhost:8080" if backend == "llamacpp" else "http://localhost:11434")
    loop = AgentLoop(
        goals=goals,
        model=config.get("model") or None,
        base_url=base_url,
        backend=backend,
        dry_run=args.dry_run or config.get("dry_run", False),
    )
    result = loop.run_once(trigger="manual")
    print(json.dumps(result, indent=2))


def cmd_doctor(args):
    from karya.core.hw_detect import print_hw_report, get_ram_gb, detect_tier
    from karya.backends.ollama import OllamaBackend
    from karya.backends.llamacpp import LlamaCppBackend

    print("\n  karya doctor")
    print("  " + "─" * 44)

    tier = print_hw_report()

    # check Ollama
    ollama_url = "http://localhost:11434"
    ob = OllamaBackend(model=tier.recommended_model, base_url=ollama_url)
    print(f"\n  checking Ollama at {ollama_url} ...")
    if ob.is_available():
        print("  [ok] Ollama is running")
        models = ob.list_models()
        if models:
            print(f"  [ok] models: {', '.join(models[:5])}")
            if tier.recommended_model in models:
                print(f"  [ok] recommended model present: {tier.recommended_model}")
            else:
                print(f"  [!]  pull recommended model:  ollama pull {tier.recommended_model}")
        else:
            print(f"  [!]  no models. run: ollama pull {tier.recommended_model}")
    else:
        print("  [!]  Ollama not running")
        print("       start:  ollama serve")
        print(f"       model:  ollama pull {tier.recommended_model}")

    # check llama-server (optional)
    llamacpp_url = "http://localhost:8080"
    lb = LlamaCppBackend(base_url=llamacpp_url)
    print(f"\n  checking llama-server at {llamacpp_url} ...")
    if lb.is_available():
        info = lb.get_model_info()
        model_name = info.get("model_path", "unknown")
        print(f"  [ok] llama-server running: {model_name}")
        print(f"       use with: karya start --backend llamacpp")
    else:
        print("  [--] llama-server not running (optional, lower overhead than Ollama)")
        print("       install:  https://github.com/ggerganov/llama.cpp")
        print(f"       launch :  llama-server -m model.gguf -c {tier.max_ctx_tokens} --port 8080")

    # priority ranking demo
    from karya.core.priority import GoalPrioritizer
    print("\n  priority ranker self-test ...")
    p = GoalPrioritizer()
    test_goals = [
        "keep disk usage below 85%",
        "restart nginx if it stops",
        "log metrics every cycle",
        "alert if CPU temperature exceeds 75°C",
    ]
    test_facts = {"disk_used_pct": 91, "cpu_temp_c": 72}
    ranked = p.rank(test_goals, current_facts=test_facts, trigger_source="threshold:disk")
    print("  ranked goals (highest first):")
    for i, sg in enumerate(ranked):
        urgency = "URGENT" if sg.score >= 60 else "HIGH" if sg.score >= 30 else "normal"
        print(f"    #{i+1} [{urgency:6s}] score={sg.score:5.1f}  {sg.goal}")
    # show HIL status
    hil_cfg = {}
    try:
        import yaml
        with open("config/goals.yaml") as f:
            hil_cfg = yaml.safe_load(f).get("hil", {})
    except Exception:
        pass
    hil_enabled = hil_cfg.get("enabled", False)
    hil_channel = hil_cfg.get("channel", "file")
    print(f"\n  HIL (human-in-the-loop):")
    if hil_enabled:
        print(f"  [ok] enabled — channel: {hil_channel} | timeout: {hil_cfg.get('timeout_sec',120)}s")
    else:
        print(f"  [--] disabled (set hil.enabled: true in goals.yaml to enable)")
    print()


def cmd_status(args):
    from karya.core.state import StateManager
    state = StateManager()
    s = state.get()
    print("\n  world state")
    print("  " + "─" * 44)
    print(f"  cycle count : {s.cycle_count}")
    print(f"  last updated: {s.last_updated}")
    print(f"\n  goals ({len(s.goals)}):")
    for g in s.goals:
        print(f"    • {g}")
    print(f"\n  facts ({len(s.facts)}):")
    for k, v in s.facts.items():
        print(f"    {k}: {v}")
    print(f"\n  recent actions ({len(s.recent_actions)}):")
    for a in s.recent_actions[-5:]:
        status = "✓" if a["success"] else "✗"
        print(f"    {status} [{a['timestamp']}] {a['tool']} → {a['result'][:60]}")
    print()


def cmd_bench(args):
    from karya.backends.ollama import OllamaBackend
    from karya.core.hw_detect import detect_tier

    tier = detect_tier()
    model = args.model or tier.recommended_model
    backend = OllamaBackend(model=model)

    if not backend.is_available():
        print("Ollama not running. Start with: ollama serve")
        sys.exit(1)

    print(f"\n  benchmarking {model} ...")
    messages = [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": 'Reply with exactly: {"tool": "none", "args": {}}'},
    ]
    start = time.time()
    response = backend.chat(messages, stream=False)
    elapsed = time.time() - start
    tokens = len(response.split())
    tps = round(tokens / elapsed, 1) if elapsed > 0 else 0
    print(f"  response   : {response[:80]}")
    print(f"  time       : {elapsed:.1f}s")
    print(f"  ~{tps} tokens/sec")
    print()


def _load_goals(goals_file: str) -> tuple[list[str], dict]:
    """Load goals from YAML file or return defaults."""
    defaults = [
        "keep disk usage below 85%",
        "log system metrics every cycle",
    ]
    default_config = {"base_url": "http://localhost:11434", "dry_run": False}

    path = Path(goals_file)
    if not path.exists():
        return defaults, default_config

    try:
        import yaml  # optional dep
        with open(path) as f:
            data = yaml.safe_load(f)
        goals = data.get("goals", defaults)
        config = {
            "model": data.get("ollama", {}).get("model", ""),
            "base_url": data.get("ollama", {}).get("base_url", "http://localhost:11434"),
            "dry_run": data.get("dry_run", False),
            "safe_gpio_pins": data.get("safe_gpio_pins", []),
            "thresholds": data.get("thresholds", []),
            "hil": data.get("hil", {}),
        }
        return goals, config
    except ImportError:
        # PyYAML not installed, just return defaults
        return defaults, default_config
    except Exception as e:
        print(f"[warn] could not load {goals_file}: {e}")
        return defaults, default_config


def main():
    parser = argparse.ArgumentParser(
        prog="karya",
        description="Offline autonomous agent for low-power hardware",
    )
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="start the autonomous agent loop")
    p_start.add_argument("--goals", default="config/goals.yaml")
    p_start.add_argument("--dry-run", action="store_true")
    p_start.add_argument("--backend", default="ollama", choices=["ollama","llamacpp"],
                         help="LLM backend: ollama (default) or llamacpp (direct llama-server)")
    p_start.add_argument("--base-url", default="", help="override backend URL")

    p_once = sub.add_parser("run-once", help="run one cycle and exit")
    p_once.add_argument("--goals", default="config/goals.yaml")
    p_once.add_argument("--dry-run", action="store_true")
    p_once.add_argument("--backend", default="ollama", choices=["ollama","llamacpp"])
    p_once.add_argument("--base-url", default="")

    p_doctor = sub.add_parser("doctor", help="check hardware and Ollama")

    p_status = sub.add_parser("status", help="show current world state")

    p_bench = sub.add_parser("bench", help="benchmark inference speed")
    p_bench.add_argument("--model", default="")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    commands = {
        "start": cmd_start,
        "run-once": cmd_run_once,
        "doctor": cmd_doctor,
        "status": cmd_status,
        "bench": cmd_bench,
    }

    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
