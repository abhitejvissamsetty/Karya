# karya

> कार्य — *that which must be done.*

Offline autonomous agent for low-power hardware — Raspberry Pi, cheap VPS, edge devices.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://python.org)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-62%20passing-brightgreen.svg)](.github/workflows/ci.yml)
[![PyPI](https://img.shields.io/badge/pypi-karya-blue.svg)](https://pypi.org/project/karya)

```
sensor/event → priority rank → model decides → hardware acts
      ↑                                               ↓
      └──────────────── world state ──────────────────┘
                     (JSON on disk, offline)
```

karya runs on a Raspberry Pi, reads your goals from a plain text file, and works towards them
autonomously — no internet, no cloud, no user input once started. It monitors system state,
ranks goals by urgency, calls a local LLM, and executes decisions via shell commands, file
writes, GPIO pins, or serial messages.

---

## Why karya exists

Most AI agent frameworks assume three things that are false on edge hardware:

- Cloud connectivity is always available
- A GPU is available for inference
- A human is watching and can intervene

karya inverts all three. It is designed for the reality of edge deployments: limited RAM,
slow CPUs, intermittent or zero connectivity, and unattended operation for days or weeks.

---

## Hardware requirements

karya auto-detects your RAM at startup and configures itself. No manual tuning needed.

| Tier | Min RAM | Recommended model | Context | TPS on Pi | Example device |
|------|---------|-------------------|---------|-----------|----------------|
| nano | < 1.5 GB | tinyllama:1.1b Q4_K_M | 512 tokens | 2–4 | Pi Zero 2W |
| micro | 1.5 GB | qwen2.5:1.5b Q4_K_M | 2048 tokens | 8–12 | Pi 4 (4GB) |
| small | 3.5 GB | qwen2.5:3b Q4_K_M | 4096 tokens | 4–7 | Pi 4/5 (8GB) |
| base | 7 GB | qwen2.5:7b Q4_K_M | 8192 tokens | 2–4 | Pi 5 (16GB) |

---

## Install

```bash
# Option A — pip
pip install karya

# Option B — pipx (isolated, recommended)
pipx install karya

# Option C — from source
git clone https://github.com/yourusername/karya
cd karya
pip install -e .

# With optional extras
pip install karya[all]
```

---

## Quickstart

```bash
# 1. Start Ollama with the right context length for your Pi
OLLAMA_CONTEXT_LENGTH=4096 ollama serve

# 2. Pull a model (auto-recommended based on your RAM)
ollama pull qwen2.5:1.5b

# 3. Check everything is working
karya doctor

# 4. Edit your goals
nano config/goals.yaml

# 5. Dry run — see decisions without executing anything
karya run-once --dry-run

# 6. Start the autonomous loop
karya start

# 7. Check current world state at any time
karya status
```

---

## goals.yaml — how you give it a mission

This is the only file you need to edit. No code changes required.

```yaml
goals:
  - "keep disk usage below 85% — clean /tmp if needed"
  - "restart nginx if it stops — critical"
  - "alert if CPU temperature exceeds 75°C"
  - "log system metrics every cycle to /var/log/karya/metrics.csv"

constraints:
  - "never delete files in /home or /etc"
  - "never stop services — only restart them"
  - "if unsure, log and skip"

cycle_interval_seconds: 30
dry_run: false

# GPIO pins karya is allowed to WRITE to (empty = GPIO disabled)
safe_gpio_pins: [18, 23, 24]

# Threshold triggers — fire immediately when a metric is breached
thresholds:
  - metric: disk_used_pct
    op: ">"
    value: 85
    check_every: 60
  - metric: cpu_temp_c
    op: ">"
    value: 75
    check_every: 10

ollama:
  base_url: "http://localhost:11434"
  model: ""   # leave blank for auto-detect from hardware tier
```

---

## How it works

Every cycle, karya runs a four-stage loop:

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌─────────────┐
│  PERCEPTION │ →  │   PRIORITY   │ →  │   DECISION   │ →  │   ACTION    │
│             │    │              │    │              │    │             │
│ Read CPU,   │    │ Rank goals   │    │ LLM receives │    │ Execute:    │
│ memory,     │    │ by urgency.  │    │ compact      │    │ shell cmd,  │
│ disk, temp, │    │ Breaching    │    │ prompt with  │    │ file write, │
│ GPIO, serial│    │ metrics top. │    │ top goal.    │    │ GPIO write, │
│             │    │ Failed goals │    │ Decides ONE  │    │ serial msg  │
│             │    │ boosted.     │    │ JSON action. │    │             │
└─────────────┘    └──────────────┘    └──────────────┘    └─────────────┘
        ↑                                                         │
        └─────────────────── world state (disk) ─────────────────┘
```

The **world state** is a compact JSON file on disk updated after every action. It survives
reboots and is serialised to under 300 tokens for injection into every prompt — giving the
model continuity without any neural summarisation or second LLM call.

---

## Multi-goal priority ranking

When multiple goals exist, karya scores every goal on five dimensions and tackles the most
urgent one first. No guessing, no round-robin.

| Signal | Max pts | Description |
|--------|---------|-------------|
| Keyword urgency | +50 | "critical", "emergency", "down", "fail" in goal text |
| Metric proximity | +60 | Currently breaching threshold — disk=91% with limit=85% |
| Trigger source | +40 | Threshold/GPIO trigger outranks scheduled cron tick |
| Failure history | +25 | Recently failed goals boosted for retry |
| Staleness | +20 | Goals not acted on in 30+ minutes gradually rise |

Example output with disk at 91% and a threshold trigger firing:

```
  priority: [URGENT] keep disk below 85% — clean /tmp if needed (score=105.0)

  #1 [URGENT] 105.0  keep disk below 85% — clean /tmp if needed
              +60 BREACHING: disk_used_pct=91.0
              +40 trigger match: threshold
              +5  keyword 'low'
  #2 [URGENT]  90.0  restart nginx if it stops — critical
              +50 keyword 'critical'
              +25 recently failed — retry urgency
  #3 [HIGH  ]  30.0  alert if CPU temperature exceeds 75°C
  #4 [normal]  15.0  log system metrics every cycle
```

---

## Triggers

karya wakes up on any of these events — not just on a timer.

| Trigger | Source | Example use |
|---------|--------|-------------|
| Cron | Built-in interval | Wake every 30s for routine checks |
| File watch | inotify / polling | Drop a `.txt` file → karya reads and acts on it |
| Threshold | /proc metrics | Disk > 85%, temp > 75°C, RAM > 90% |
| GPIO | RPi.GPIO / gpiozero / sysfs | Button press, PIR sensor, relay feedback |
| Serial | UART / USB | Arduino sensor message, RS-485 device alert |

### File watch — the dead drop interface

Drop a plain text file into `~/.karya/tasks/` and karya wakes up, reads it, acts on it,
and moves it to `done/`. Works offline, no network, no terminal needed.

```bash
echo "check disk and delete old logs immediately" > ~/.karya/tasks/cleanup.txt
# karya picks it up within 2 seconds
```

---

## Tools

Every tool works with zero network. The safety guard checks every action before execution.

| Tool | Actions | Notes |
|------|---------|-------|
| `shell` | run any shell command | forbidden patterns enforced, confirm delay for destructive cmds |
| `file` | read, write, append, exists | write restricted to allowed directories |
| `system_info` | cpu, memory, disk, temp, processes | reads `/proc` directly, no psutil required |
| `gpio` | read, write, pulse | pin whitelist required for writes |
| `serial` | send, read, send_and_read | any UART/USB device, requires pyserial |

---

## Safety guard rails

Every action passes through the safety layer before execution. This cannot be bypassed.

**FORBIDDEN — never execute:**
- `rm -rf /`, `rm -rf ~`, `dd if=...`, fork bombs
- Remote code execution via `curl | sh` or `wget | bash`
- Writes to `/boot`, `/etc/passwd`, `/etc/shadow`, `/proc`, `/sys`, `/dev`

**CONFIRM — 10-second pause, then execute:**
- `rm`, `kill`, `pkill`, `systemctl stop`, `systemctl disable`
- `chmod`, `chown`

**GPIO — write only to whitelisted pins:**
```yaml
safe_gpio_pins: [18, 23, 24]  # in goals.yaml
```

**Dry-run mode:**
```bash
karya start --dry-run   # log everything, execute nothing
```

---

## LLM backends

### Ollama (recommended)

```bash
OLLAMA_CONTEXT_LENGTH=4096 ollama serve
ollama pull qwen2.5:1.5b
karya start
```

> **Pi-specific:** Ollama defaults to 4096 tokens for all models regardless of capability.
> Always set `OLLAMA_CONTEXT_LENGTH` to match your tier — 2048 for Pi Zero,
> 4096 for Pi 4 (4GB), 8192 for Pi 5 (8GB+).

Bake it into a Modelfile for a permanent fix:

```bash
echo -e "FROM qwen2.5:1.5b\nPARAMETER num_ctx 4096" > Modelfile
ollama create qwen2.5-4k -f Modelfile
```

### llama.cpp direct (lower overhead, better for Pi Zero)

```bash
llama-server -m ~/models/qwen2.5-1.5b-q4_k_m.gguf \
  -c 2048 --port 8080 \
  --cache-type-k q4_0 --cache-type-v q4_0   # saves RAM
karya start --backend llamacpp
```

---

## Context management

karya fits conversation history into the hardware token budget with a deterministic
four-step algorithm — no LLM calls, no neural summarisation:

1. **Truncate tool results first** — largest tokens, lowest long-term value
2. **Drop oldest turn pairs together** — never orphan a tool call
3. **Shorten system prompt** if still over budget
4. **Hard truncate current message** as last resort

### Tool call fallback parser

Small models (1B–3B) frequently break JSON. The parser tries four methods before giving up:

1. `json.loads()` on raw response
2. Extract from ` ```json ... ``` ` code block
3. Regex on bare JSON object in the text
4. Keyword detection in plain text

---

## Run on boot (Raspberry Pi)

```bash
sudo cp systemd/karya.service /etc/systemd/system/
sudo nano /etc/systemd/system/karya.service   # set your username and paths
sudo systemctl daemon-reload
sudo systemctl enable karya
sudo systemctl start karya

# Follow logs
sudo journalctl -u karya -f
```

---

## CLI reference

```
karya start              Start autonomous loop (runs forever)
  --goals FILE           Path to goals.yaml (default: config/goals.yaml)
  --dry-run              Log decisions without executing anything
  --backend ollama|llamacpp
  --base-url URL         Override backend URL

karya run-once           Run exactly one cycle and exit
  --goals FILE
  --dry-run
  --backend ollama|llamacpp

karya doctor             Check hardware tier, Ollama, llama-server, run priority demo
karya status             Show world state: goals, facts, recent actions
karya bench              Measure tokens/sec on current hardware
  --model MODEL
```

---

## Use cases

### Home and personal

**Self-healing home server** — Pi 4 running nginx, Pi-hole, Plex. karya monitors all three,
cleans disk when it fills, restarts services on crash. No Home Assistant, no cloud.

**Smart power manager** — Pi Zero connected to smart plug relays. Reads power draw, cuts
power to idle devices overnight, logs consumption. Hardware cost under £30.

**Offline security system** — PIR sensors trigger karya. Decides whether it is a real
intrusion based on time of day, prior events, and door sensor state.

### Agriculture and environment

**Autonomous irrigation brain** — deployed in a field with no mobile signal. Reads soil
moisture, temperature, and light sensors. Opens and closes irrigation valves based on crop
goals. Runs for weeks without maintenance.

**Greenhouse climate agent** — manages heaters, ventilation fans, grow lights, and humidity
simultaneously. When multiple goals compete, the priority ranker decides which matters most.

**Air quality guardian** — monitors CO2, VOC, and particulates in a sealed building with no
internet. Activates ventilation on degraded air, closes intakes during pollution events.

### Industrial and infrastructure

**Predictive maintenance node** — accelerometer on serial port reads vibration signatures
from factory equipment. Detects deviation from baseline, writes a maintenance alert file.

**Remote pipeline monitor** — deployed at oil, gas, or water sites with zero connectivity.
Reads pressure and flow sensors, controls shutoff valves via GPIO. Designed for months
unattended.

**Edge server watchdog** — no GPIO needed. Runs as a systemd service on any Linux server.
Monitors disk, memory, temperature, and services. Cleans logs, restarts crashed processes.

### Robotics and field deployment

**Robot high-level brain** — sits on top of an Arduino or Pixhawk via serial. Receives
sensor state, decides the next high-level action, sends commands back.

**Field data collector** — a researcher drops a `.txt` task file onto a USB drive. The file
watch trigger fires, karya executes the instructions and writes results back to the drive.

**Wildlife monitoring station** — solar-powered, deployed for months. PIR triggers karya on
motion detection. Logs a timestamped event, triggers a camera, writes a structured record.

---

## Architecture

```
karya/
├── karya/
│   ├── core/
│   │   ├── hw_detect.py     # RAM → tier → token budgets
│   │   ├── state.py         # world state (JSON, survives reboots)
│   │   ├── context.py       # sliding window trimmer, no LLM needed
│   │   ├── priority.py      # multi-goal urgency ranker
│   │   ├── safety.py        # command guard rails
│   │   └── loop.py          # perception → decision → action
│   ├── backends/
│   │   ├── ollama.py        # streaming + 4-level tool call parser
│   │   └── llamacpp.py      # direct llama-server (lower overhead)
│   ├── tools/
│   │   ├── __init__.py      # ShellTool, FileTool, SystemInfoTool
│   │   ├── gpio.py          # hardware pin read/write/pulse
│   │   └── serial_tool.py   # UART/USB serial communication
│   ├── triggers/
│   │   ├── base.py          # BaseTrigger + TriggerEvent
│   │   ├── cron.py          # interval heartbeat
│   │   ├── file_watch.py    # inotify / poll directory
│   │   ├── threshold.py     # metric breach detection with hysteresis
│   │   ├── gpio.py          # hardware pin event (RPi.GPIO/gpiozero/sysfs)
│   │   └── serial.py        # serial port listener
│   └── cli.py               # start / run-once / doctor / status / bench
├── config/
│   └── goals.yaml           # define goals here, no code needed
├── systemd/
│   └── karya.service        # boot-time autostart
├── tests/                   # 62 tests
├── .github/workflows/ci.yml # Python 3.10 / 3.11 / 3.12
└── pyproject.toml
```

---

## How it compares

| Feature | karya | Hermes Agent | Qwen-Agent | LocalAI | Home Assistant |
|---------|-------|--------------|------------|---------|----------------|
| Min RAM | 512 MB | 8 GB+ | 4 GB+ | 4 GB+ | 2 GB+ |
| Fully offline | ✅ | Partial | ❌ | ✅ | Partial |
| No user input needed | ✅ | ❌ | ❌ | ❌ | Partial |
| GPIO / serial tools | ✅ | ❌ | ❌ | ❌ | ✅ |
| Priority ranking | ✅ | ❌ | ❌ | ❌ | Rule-based |
| Context compression | Rule-based | Neural LLM call | Basic | None | N/A |
| Zero required deps | ✅ | ❌ | ❌ | ❌ | ❌ |
| Pi Zero support | ✅ | ❌ | ❌ | ❌ | ❌ |
| Goals in plain English | ✅ | ✅ | Code only | ❌ | ❌ |

---

## Optional dependencies

The core agent has zero required dependencies — stdlib only.

```bash
pip install karya           # core only
pip install karya[full]     # + pyyaml + psutil
pip install karya[serial]   # + pyserial
pip install karya[gpio]     # + RPi.GPIO + gpiozero (Pi only)
pip install karya[all]      # everything
```

---

## Roadmap

- [x] Hardware tier detection + token budgets
- [x] World state (disk-based, survives reboots)
- [x] Context manager (rule-based sliding window)
- [x] Multi-goal priority ranker (5 signals, no LLM needed)
- [x] Safety guard rails
- [x] Ollama backend with streaming
- [x] llama.cpp direct backend
- [x] Shell + file + system + GPIO + serial tools
- [x] Cron + file_watch + threshold + GPIO + serial triggers
- [x] CLI (start / run-once / doctor / status / bench)
- [x] Systemd service
- [x] 62-test suite (Python 3.10 / 3.11 / 3.12)
- [x] PyPI packaging
- [ ] llama.cpp subprocess backend (no server needed at all)
- [ ] Multi-agent cluster over local network
- [ ] Wake-word voice trigger via whisper.cpp
- [ ] Lightweight local web dashboard
- [ ] Plugin SDK for custom tools and triggers

---

## Contributing

Pull requests are welcome. For major changes, open an issue first.

```bash
git clone https://github.com/yourusername/karya
cd karya
pip install -e ".[all]"
python -m pytest          # 62 tests
```

Branch naming: `feat/`, `fix/`, `docs/`, `test/`

---

## Name

**karya** comes from Sanskrit कार्य — *work, task, that which must be done.* A goal-driven agent that executes its tasks autonomously, without being told twice.

---

## License

MIT — see [LICENSE](LICENSE).

karya does not bundle any model weights. Models must be pulled separately via Ollama or downloaded as GGUF files from Hugging Face. Recommended models — Qwen2.5 (Apache 2.0), TinyLlama (Apache 2.0), Gemma 3 (Gemma Terms of Use) — are each released under their own open-source licenses.

---

## Acknowledgements

Built on [Ollama](https://ollama.com) and [llama.cpp](https://github.com/ggerganov/llama.cpp).
Recommended models: [Qwen2.5](https://huggingface.co/Qwen) · [TinyLlama](https://huggingface.co/TinyLlama) · [Gemma 3](https://ai.google.dev/gemma).
