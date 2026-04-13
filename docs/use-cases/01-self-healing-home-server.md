# Use case 1 — self-healing home server

## What it does

A Raspberry Pi 4 running nginx, Pi-hole, and Plex monitors itself and acts when something goes wrong. If a service crashes, disk fills up, or CPU temperature spikes — karya detects it and takes corrective action. No Home Assistant, no cloud dashboard, no manual SSH needed.

---

## Who this is for

- Home lab users running self-hosted services on a Pi
- Anyone who wants their server to fix itself overnight without waking them up
- Developers testing karya for the first time — this needs no GPIO hardware

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi 4 (4GB or 8GB) |
| Model | `qwen2.5:1.5b` (4GB) or `qwen2.5:3b` (8GB) |
| Connectivity | LAN only — no internet required after setup |
| Storage | 32GB+ microSD or USB SSD |

---

## What you need before starting

- [ ] Raspberry Pi 4 with Raspberry Pi OS (64-bit, Bookworm)
- [ ] Ollama installed on the Pi
- [ ] karya installed (`pip install karya`)
- [ ] nginx, Pi-hole, or any service already running

---

## Step-by-step setup

### Step 1 — Install Ollama and pull the model

```bash
curl -fsSL https://ollama.com/install.sh | sh
OLLAMA_CONTEXT_LENGTH=4096 ollama serve &
ollama pull qwen2.5:1.5b
```

### Step 2 — Install karya

```bash
pip install karya
# or for isolated install:
pipx install karya
```

### Step 3 — Verify everything works

```bash
karya doctor
```

You should see your hardware tier detected as `micro`, the recommended model confirmed, and Ollama listed as running.

### Step 4 — Create your goals file

```bash
mkdir -p ~/karya/config
nano ~/karya/config/goals.yaml
```

Paste this and edit to match the services you actually run:

```yaml
goals:
  - "keep disk usage below 85% — delete files in /tmp and /var/log if needed"
  - "restart nginx if it stops running"
  - "restart pihole-FTL if it stops running"
  - "alert if CPU temperature exceeds 75°C by writing to /tmp/karya_alert.txt"
  - "log system metrics every cycle to /var/log/karya/metrics.csv"

constraints:
  - "never delete files in /home or /etc"
  - "never run apt, apt-get, or pip"
  - "never stop or disable services — only restart them"
  - "if unsure about an action, log it and skip"

cycle_interval_seconds: 30
dry_run: false
safe_gpio_pins: []

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 5 — Dry run first

Always test before letting it act:

```bash
cd ~/karya
karya run-once --dry-run --goals config/goals.yaml
```

Read the output. Confirm karya is ranking goals correctly and proposing sensible actions.

### Step 6 — Run one live cycle

```bash
karya run-once --goals config/goals.yaml
```

Watch what it does. Check `/var/log/karya/` for the metrics log.

### Step 7 — Start the autonomous loop

```bash
karya start --goals config/goals.yaml
```

karya now wakes every 30 seconds, checks the system, and acts if any goal needs attention.

### Step 8 — Run on boot with systemd

```bash
sudo cp systemd/karya.service /etc/systemd/system/

# Edit to set the correct user and paths
sudo nano /etc/systemd/system/karya.service

sudo systemctl daemon-reload
sudo systemctl enable karya
sudo systemctl start karya

# Confirm it's running
sudo systemctl status karya

# Follow live logs
sudo journalctl -u karya -f
```

---

## What karya does in practice

| Situation | karya's action |
|-----------|---------------|
| Disk reaches 86% | Runs `find /tmp -mtime +7 -delete`, then `journalctl --vacuum-size=200M` |
| nginx returns no process | Runs `systemctl restart nginx`, logs result |
| CPU temp hits 76°C | Writes alert to `/tmp/karya_alert.txt`, logs cycle |
| All goals met | Logs "no action needed", waits for next cycle |

---

## Check current world state

At any time, run:

```bash
karya status
```

This shows the last 5 actions taken, known facts (current disk %, temperature, service statuses), and pending goals.

---

## Troubleshooting

**karya is restarting nginx every cycle**
karya may be misreading the service status. Add to constraints: `"only restart nginx if systemctl is-active nginx returns inactive or failed"`.

**Disk is filling faster than karya can clean**
Lower the threshold: `"keep disk below 75%"` and add more specific cleanup targets in the goal description.

**karya doctor shows Ollama not running**
Run `OLLAMA_CONTEXT_LENGTH=4096 ollama serve` in a separate terminal, or add it to your systemd environment.

---

## Next steps

Once this is working, extend it:
- Add a threshold trigger so karya wakes instantly when disk hits 85% instead of waiting for the cron cycle
- Add a file watch trigger so you can drop a task file to run ad-hoc commands
- Try the `karya bench` command to see your tokens/sec and tune the cycle interval
