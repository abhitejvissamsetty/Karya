# karya

> कार्य — *that which must be done.*

Offline autonomous agent for low-power hardware — Raspberry Pi, cheap VPS, edge devices.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://python.org)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-142%20passing-brightgreen.svg)](.github/workflows/ci.yml)
[![PyPI](https://img.shields.io/badge/pypi-karya-blue.svg)](https://pypi.org/project/karya)

```
sensor/event → priority rank → model decides → hardware acts
      ↑                                               ↓
      └──────────────── world state ──────────────────┘
                     (JSON on disk, fully offline)
```

karya runs on a Raspberry Pi, reads your goals from a plain text file, and works towards them autonomously — no internet, no cloud, no user input once started. It monitors system state, ranks goals by urgency, calls a local LLM, and executes decisions via shell commands, file writes, GPIO pins, or serial messages.

---

## Why karya exists

Most AI agent frameworks assume three things that are false on edge hardware:

- Cloud connectivity is always available
- A GPU is available for inference
- A human is watching and can intervene

karya inverts all three. It is designed for the reality of edge deployments: limited RAM, slow CPUs, intermittent or zero connectivity, and unattended operation for days or weeks at a time.

> **Full reasoning:** [Why an LLM and not just cron + scripts?](docs/why-llm-not-scripts.md)

---

## Hardware requirements

karya auto-detects your RAM at startup and configures token budgets automatically. No manual tuning needed.

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

# With all optional extras
pip install karya[all]
```

---

## Quickstart

```bash
# 1. Start Ollama with correct context length for your Pi
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

# Threshold triggers — fire karya immediately when a metric is breached
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

The **world state** is a compact JSON file on disk, updated after every action. It survives reboots and is serialised to under 300 tokens for injection into every LLM prompt — giving the model continuity without any neural summarisation or second LLM call.

---

## Multi-goal priority ranking

When multiple goals exist, karya scores every goal on five dimensions and tackles the most urgent one first. No guessing, no round-robin — the highest scoring goal always leads.

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
| Threshold | `/proc` metrics | Disk > 85%, temp > 75°C, RAM > 90% |
| GPIO | RPi.GPIO / gpiozero / sysfs | Button press, PIR sensor, relay feedback |
| Serial | UART / USB | Arduino sensor message, RS-485 device alert |

### File watch — the dead drop interface

Drop a plain text file into `~/.karya/tasks/` and karya wakes up, reads it, acts on it, and moves it to `done/`. Works fully offline — no network, no terminal, no SSH needed.

```bash
echo "check disk and delete old logs immediately" > ~/.karya/tasks/cleanup.txt
# karya picks it up within 2 seconds
```

---

## Tools

Every tool works with zero network. The safety guard checks every action before execution.

| Tool | Actions | Notes |
|------|---------|-------|
| `shell` | run any shell command | Forbidden patterns enforced, confirm delay for destructive cmds |
| `file` | read, write, append, exists | Write restricted to allowed directories |
| `system_info` | cpu, memory, disk, temp, processes | Reads `/proc` directly — no psutil required |
| `gpio` | read, write, pulse | Pin whitelist required for writes |
| `serial` | send, read, send_and_read | Any UART/USB device — requires pyserial |

---

## Safety guard rails

Every action passes through the safety layer before execution. This cannot be bypassed.

**FORBIDDEN — never execute, ever:**
- `rm -rf /`, `rm -rf ~`, `dd if=...`, fork bombs
- Remote code execution via `curl | sh` or `wget | bash`
- Writes to `/boot`, `/etc/passwd`, `/etc/shadow`, `/proc`, `/sys`, `/dev`

**CONFIRM — 10-second pause, then execute:**
- `rm`, `kill`, `pkill`, `systemctl stop`, `systemctl disable`
- `chmod`, `chown`

**GPIO — write only to explicitly whitelisted pins:**
```yaml
safe_gpio_pins: [18, 23, 24]  # in goals.yaml
```

**Dry-run mode — log everything, execute nothing:**
```bash
karya start --dry-run
```

---

## Human-in-the-loop (HIL) for critical decisions

karya is offline-first. Its HIL system is too.

Before executing any critical action, karya pauses and waits for human approval. Offline channels are the default. Internet channels are entirely optional — only for users who happen to have connectivity and want faster mobile notifications.

```
AUTO     →  execute immediately        (df -h, system_info, read_file)
CONFIRM  →  10s pause, then execute    (chmod, mv, chown)
CRITICAL →  pause, notify, wait       (rm, gpio write, systemctl stop, score >= 80)
BLOCK    →  never execute             (rm -rf /, dd if=, fork bombs)
```

### Channels — offline first

| Channel | Internet | How you respond |
|---------|----------|-----------------|
| `file` *(default)* | ❌ No | `touch ~/.karya/hil/approved/<id>.approve` |
| `display` | ❌ No | Type `y` or `n` on attached keyboard or SSH session |
| `gpio_button` | ❌ No | Press a physical green/red button wired to Pi |
| `serial` | ❌ No | Type `approve` or `deny` on a UART terminal |
| `telegram` | ✅ Optional | Tap inline approve/deny buttons on your phone |
| `slack` | ✅ Optional | Reply `approve <id>` in a Slack channel |
| `webhook` | ✅ Optional | Any HTTP service — n8n, Home Assistant, Zapier |

karya never requires internet for HIL. If you configure an online channel but connectivity is unavailable, it falls back to `file` automatically — it never blocks on a missing network.

### Enable in goals.yaml

```yaml
hil:
  enabled: true
  timeout_sec: 120
  default_on_timeout: deny   # "deny" (safe) or "approve" (permissive)

  # ── Offline channels (no internet needed) ─────────────────────────────────

  channel: file              # DEFAULT — zero dependencies, always works
  hil_dir: "~/.karya/hil"

  # channel: display         # keypress on attached screen or SSH terminal
  
  # channel: gpio_button     # physical buttons wired to Pi GPIO pins
  # approve_pin: 5           # BCM — green button, active LOW
  # deny_pin: 6              # BCM — red button, active LOW
  # led_pin: 13              # optional LED — blinks while waiting

  # channel: serial          # type approve/deny on a UART terminal
  # serial_port: "/dev/ttyUSB0"
  # serial_baud: 115200

  # ── Optional online channels ───────────────────────────────────────────────

  # channel: telegram
  # telegram_bot_token: "123456:ABC-your-bot-token"
  # telegram_chat_id: "987654321"

  # channel: slack
  # slack_webhook_url: "https://hooks.slack.com/services/..."

  # channel: webhook
  # webhook_notify_url: "https://your-server/karya/notify"
  # webhook_poll_url:   "https://your-server/karya/decision/{request_id}"
```

### How the file channel works (default, fully offline)

karya writes a pending JSON file. You respond by creating a file or appending a line:

```bash
# Approve
touch ~/.karya/hil/approved/<request_id>.approve

# Deny
touch ~/.karya/hil/denied/<request_id>.deny

# Or use the text file
echo "approve <request_id>" >> ~/.karya/hil/responses.txt
```

### How the GPIO button channel works (Pi hardware, fully offline)

Wire two momentary buttons to Pi GPIO pins. No internet, no terminal, no files needed. The human physically walks up to the device and presses a button. An optional LED blinks while karya waits.

```
Green button → GPIO pin 5  (approve)
Red button   → GPIO pin 6  (deny)
LED          → GPIO pin 13 (blinks while waiting — optional)
```

### How the display channel works (screen or SSH, fully offline)

karya prints the pending action to the terminal and waits for a keypress. Works in any SSH session or on a device with an attached keyboard.

```
════════════════════════════════════════════════════
  karya — CRITICAL ACTION — approval required
════════════════════════════════════════════════════
  Tool   : shell
  Args   : {"command": "rm /var/log/nginx/access.log.1"}
  Goal   : keep disk below 85%
  Reason : command contains 'rm '
  Score  : 91
  Timeout: 120s
════════════════════════════════════════════════════
  Approve? [y/N]:
```

### Audit log

Every HIL decision — approved, denied, or timed out — is recorded in `~/.karya/hil/log/hil_audit.jsonl` with full context: tool, args, goal, priority score, reason flagged, who decided, and when.

---

## LLM backends

### Ollama (recommended)

```bash
# Critical on Pi — set context length before starting
OLLAMA_CONTEXT_LENGTH=4096 ollama serve
ollama pull qwen2.5:1.5b
karya start
```

> **Pi-specific:** Ollama defaults to 4096 tokens for all models regardless of their actual capability. Always set `OLLAMA_CONTEXT_LENGTH` to match your hardware tier — 2048 for Pi Zero, 4096 for Pi 4 (4GB), 8192 for Pi 5 (8GB+). Without this, karya's context budgets will be wrong.

Bake it permanently into a Modelfile:

```bash
echo -e "FROM qwen2.5:1.5b\nPARAMETER num_ctx 4096" > Modelfile
ollama create qwen2.5-4k -f Modelfile
```

### llama.cpp direct (lower overhead, best for Pi Zero)

Connects directly to `llama-server` — no Ollama layer. Saves ~150MB RAM. Better for Pi Zero and any device where every megabyte counts.

```bash
llama-server -m ~/models/qwen2.5-1.5b-q4_k_m.gguf \
  -c 2048 --port 8080 \
  --cache-type-k q4_0 --cache-type-v q4_0   # quantised KV cache saves RAM
karya start --backend llamacpp
```

---

## Context management

karya fits conversation history into the hardware token budget with a deterministic four-step algorithm — no LLM calls, no neural summarisation, no second model:

1. **Truncate tool results first** — largest tokens, lowest long-term value
2. **Drop oldest turn pairs together** — never orphan a tool call
3. **Shorten system prompt** if still over budget
4. **Hard truncate current message** as last resort

### Tool call fallback parser

Small models (1B–3B) frequently break JSON formatting. The parser tries four methods before giving up:

1. `json.loads()` on raw response
2. Extract from ` ```json ... ``` ` code block
3. Regex on bare JSON object anywhere in the text
4. Keyword detection in plain text ("run ...", "check system")

---

## Run on boot (Raspberry Pi)

```bash
sudo cp systemd/karya.service /etc/systemd/system/
sudo nano /etc/systemd/system/karya.service   # set your username and paths
sudo systemctl daemon-reload
sudo systemctl enable karya
sudo systemctl start karya

# Follow live logs
sudo journalctl -u karya -f
```

---

## CLI reference

```
karya start                 Start autonomous loop — runs forever
  --goals FILE              Path to goals.yaml (default: config/goals.yaml)
  --dry-run                 Log all decisions without executing anything
  --backend ollama|llamacpp LLM backend (default: ollama)
  --base-url URL            Override backend URL

karya run-once              Run exactly one cycle and exit
  --goals FILE
  --dry-run
  --backend ollama|llamacpp

karya doctor                Check hardware tier, Ollama, llama-server, HIL status
karya status                Show world state: goals, facts, recent actions
karya bench                 Measure tokens/sec on current hardware
  --model MODEL
```

---

## Use cases

Step-by-step deployment guides with exact hardware wiring, goals.yaml config, and a table of what karya does in each scenario.

| Guide | Device | Start here if... |
|-------|--------|------------------|
| [Edge server watchdog](docs/use-cases/03-edge-server-watchdog.md) | Any Linux | No GPIO hardware — just want to try karya |
| [Self-healing home server](docs/use-cases/01-self-healing-home-server.md) | Pi 4 (4GB) | Running self-hosted services on a Pi |
| [Autonomous irrigation](docs/use-cases/02-autonomous-irrigation.md) | Pi 4 (4GB) | Have a garden, greenhouse, or field plot |
| [Offline intruder response](docs/use-cases/04-offline-intruder-response.md) | Pi 4 (4GB) | Want a context-aware alarm with no cloud |
| [Predictive maintenance](docs/use-cases/07-predictive-maintenance.md) | Pi 4 (4GB) | Monitoring motors or industrial machinery |
| [Field data collector](docs/use-cases/06-field-data-collector.md) | Pi 4 (4GB) | Need to send tasks to a remote device |
| [Wildlife monitoring station](docs/use-cases/05-wildlife-monitoring.md) | Pi Zero 2W | Deploying for months in the field |

---

## Architecture

```
karya/
├── karya/
│   ├── core/
│   │   ├── hw_detect.py     # RAM → tier → token budgets (auto)
│   │   ├── state.py         # world state (JSON on disk, survives reboots)
│   │   ├── context.py       # sliding window trimmer — no LLM needed
│   │   ├── priority.py      # multi-goal urgency ranker (5 signals)
│   │   ├── safety.py        # command guard rails (forbidden/confirm/block)
│   │   ├── hil.py           # human-in-the-loop approval system
│   │   └── loop.py          # perception → priority → decision → action
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
│   └── goals.yaml           # define goals here — no code needed
├── systemd/
│   └── karya.service        # boot-time autostart
├── docs/
│   ├── why-llm-not-scripts.md
│   └── use-cases/           # 7 step-by-step deployment guides
├── tests/                   # 142 tests
├── .github/workflows/ci.yml # Python 3.10 / 3.11 / 3.12
└── pyproject.toml
```

---

## Why an LLM and not just cron + scripts?

The short answer: for simple, predictable failures a cron job is fine and faster. karya handles the cases scripts silently fail on.

| What breaks | cron + bash | karya |
|-------------|-------------|-------|
| Disk above threshold → clean | ✅ Works | ✅ Works |
| Service down → restart | ✅ Works | ✅ Works |
| Service down *because* disk is full → fix root cause first | ❌ Restarts, fails again | ✅ Cleans disk first |
| Sensor reading: startup transient vs real fault | ❌ Fires alert either way | ✅ Reads history, classifies |
| Three competing goals with conflicting actions | ❌ First matching rule wins | ✅ Scores all, picks best net action |
| Novel instruction from field operator via text file | ❌ Impossible | ✅ Reads and executes |
| Do not act during active backup | ❌ Acts anyway | ✅ Reads process list, waits |
| Add a new condition to monitor | ❌ Write, test, deploy new script | ✅ One line in goals.yaml |

> **Full explanation with before/after examples →** [docs/why-llm-not-scripts.md](docs/why-llm-not-scripts.md)

---

## How it compares

| Feature | karya | Hermes Agent | Qwen-Agent | LocalAI | Home Assistant |
|---------|-------|--------------|------------|---------|----------------|
| Min RAM | 512 MB | 8 GB+ | 4 GB+ | 4 GB+ | 2 GB+ |
| Fully offline | ✅ | Partial | ❌ | ✅ | Partial |
| No user input needed | ✅ | ❌ | ❌ | ❌ | Partial |
| GPIO / serial tools | ✅ | ❌ | ❌ | ❌ | ✅ |
| Priority ranking | ✅ | ❌ | ❌ | ❌ | Rule-based |
| Human-in-the-loop | ✅ offline-first | ❌ | ❌ | ❌ | ❌ |
| Context compression | Rule-based | Neural LLM call | Basic | None | N/A |
| Zero required deps | ✅ | ❌ | ❌ | ❌ | ❌ |
| Pi Zero support | ✅ | ❌ | ❌ | ❌ | ❌ |
| Goals in plain English | ✅ | ✅ | Code only | ❌ | ❌ |

---

## Optional dependencies

The core agent has zero required dependencies — pure Python stdlib.

```bash
pip install karya            # core only — stdlib, zero deps
pip install karya[full]      # + pyyaml + psutil
pip install karya[serial]    # + pyserial (serial port tools and triggers)
pip install karya[gpio]      # + RPi.GPIO + gpiozero (Pi only)
pip install karya[all]       # everything
```

| Package | License | Used for |
|---------|---------|----------|
| pyyaml | MIT | goals.yaml parsing |
| psutil | BSD-3 | System metrics on non-Linux |
| pyserial | BSD-3 | Serial port tools and triggers |
| RPi.GPIO | MIT | Raspberry Pi GPIO (Pi only) |
| gpiozero | BSD-3 | Alternative GPIO library (Pi only) |

---

## Roadmap

- [x] Hardware tier detection + token budgets
- [x] World state (disk-based, survives reboots)
- [x] Context manager (rule-based sliding window)
- [x] Multi-goal priority ranker (5 signals, no LLM needed)
- [x] Safety guard rails (forbidden / confirm / block)
- [x] Ollama backend with streaming
- [x] llama.cpp direct backend
- [x] Shell + file + system + GPIO + serial tools
- [x] Cron + file_watch + threshold + GPIO + serial triggers
- [x] Human-in-the-loop — 7 channels, offline-first
- [x] CLI (start / run-once / doctor / status / bench)
- [x] Systemd service
- [x] 82-test suite (Python 3.10 / 3.11 / 3.12)
- [x] PyPI packaging
- [ ] llama.cpp subprocess backend (no server needed at all)
- [ ] Multi-agent cluster over local network
- [ ] Wake-word voice trigger via whisper.cpp
- [ ] Lightweight local web dashboard
- [ ] Plugin SDK for custom tools and triggers

---

## Contributing

Pull requests are welcome. For major changes, open an issue first to discuss.

```bash
git clone https://github.com/yourusername/karya
cd karya
pip install -e ".[all]"
python -m pytest          # 142 tests
```

Branch naming: `feat/`, `fix/`, `docs/`, `test/`

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new tools and triggers.

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
