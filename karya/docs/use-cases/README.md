# karya — use case guides

Each guide covers a real deployment scenario with step-by-step setup, exact goals.yaml configuration, wiring diagrams where needed, and a table of what karya does in practice for that use case.

---

## Pick your scenario

### No hardware required (start here)

| Guide | Device | Difficulty |
|-------|--------|------------|
| [Edge server watchdog](./03-edge-server-watchdog.md) | Any Linux machine | Beginner |
| [Self-healing home server](./01-self-healing-home-server.md) | Pi 4 (4GB) | Beginner |

### GPIO / sensor hardware

| Guide | Device | Difficulty |
|-------|--------|------------|
| [Offline intruder response](./04-offline-intruder-response.md) | Pi 4 (4GB) | Intermediate |
| [Autonomous irrigation brain](./02-autonomous-irrigation.md) | Pi 4 (4GB) | Intermediate |
| [Predictive maintenance node](./07-predictive-maintenance.md) | Pi 4 (4GB) | Intermediate |

### Remote / offline deployment

| Guide | Device | Difficulty |
|-------|--------|------------|
| [Field data collector](./06-field-data-collector.md) | Pi 4 (4GB) | Beginner |
| [Wildlife monitoring station](./05-wildlife-monitoring.md) | Pi Zero 2W | Advanced |

---

## Common setup steps across all use cases

Every use case follows the same pattern:

```
1. Install Ollama + pull model
2. pip install karya
3. karya doctor          ← verify hardware and backend
4. Write goals.yaml
5. karya run-once --dry-run   ← test before acting
6. karya start           ← go live
7. systemctl enable karya     ← run on boot
```

The only thing that changes between use cases is `goals.yaml`.

---

## goals.yaml quick reference

```yaml
goals:
  - "plain English description of what to achieve"

constraints:
  - "things karya must never do"

cycle_interval_seconds: 30    # how often to run the background heartbeat
dry_run: false                # true = log decisions, never execute
safe_gpio_pins: [18, 23]     # GPIO pins allowed for writes (empty = disabled)

thresholds:                   # fire karya immediately when metric breaches
  - metric: disk_used_pct
    op: ">"
    value: 85
    check_every: 60

gpio_triggers:                # fire karya immediately when pin changes state
  - pin: 17
    edge: "rising"

ollama:
  base_url: "http://localhost:11434"
  model: ""    # leave blank for auto-detect
```

---

## CLI commands used in every guide

```bash
karya doctor           # check hardware, Ollama, priority ranker
karya run-once --dry-run   # simulate one cycle without acting
karya run-once         # run one real cycle and exit
karya start            # run continuously
karya status           # show world state and recent actions
karya bench            # measure tokens/sec on this hardware
```

---

## Hardware decision guide

| Available RAM | Best model | Best backend | Recommended use case to start |
|---------------|------------|--------------|-------------------------------|
| < 1 GB | tinyllama:1.1b | llama.cpp | Wildlife monitoring |
| 1.5–4 GB | qwen2.5:1.5b | Ollama | Edge server watchdog |
| 4–8 GB | qwen2.5:3b | Ollama | Self-healing home server |
| 8 GB+ | qwen2.5:7b | Ollama | Any |

---

## Contributing a use case

If you have deployed karya in a scenario not covered here, contributions are welcome. Open a PR with a new markdown file following the same structure: what it does, hardware, step-by-step setup, what karya does in practice, troubleshooting.
