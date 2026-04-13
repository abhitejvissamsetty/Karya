# Use case 3 — edge server watchdog

## What it does

karya runs as a background daemon on any Linux server — a cheap VPS, a headless mini PC, a Pi sitting in a rack. It monitors disk space, memory, CPU temperature, and critical services. When something goes wrong it takes corrective action: cleaning old logs, restarting processes, or writing an alert file. No GPIO hardware required. This is the easiest first deployment.

---

## Who this is for

- Developers running a personal VPS or home server
- Anyone who has lost data or had downtime because a disk filled up unnoticed
- Teams deploying Pi devices in remote locations (retail kiosks, info displays, sensors)

---

## Hardware

| Item | Spec |
|------|------|
| Device | Any Linux machine — Pi, VPS, old laptop |
| Min RAM | 1.5 GB |
| Model | `qwen2.5:1.5b` |
| Connectivity | None required — Ollama runs locally |

---

## What you need before starting

- [ ] Linux OS (Raspberry Pi OS, Ubuntu, Debian, Fedora)
- [ ] Python 3.10+
- [ ] Ollama installed and running locally
- [ ] karya installed

---

## Step-by-step setup

### Step 1 — Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

For a headless server, set context length and start as a background process:

```bash
OLLAMA_CONTEXT_LENGTH=4096 nohup ollama serve > /var/log/ollama.log 2>&1 &
ollama pull qwen2.5:1.5b
```

### Step 2 — Install karya

```bash
pip install karya
```

### Step 3 — Run the doctor check

```bash
karya doctor
```

Confirm your hardware tier, Ollama status, and the priority ranker demo. Everything should show green before proceeding.

### Step 4 — Create goals.yaml

```bash
mkdir -p ~/karya/config
nano ~/karya/config/goals.yaml
```

```yaml
goals:
  - "keep disk usage below 80% — vacuum journal logs and delete files in /tmp older than 7 days"
  - "if disk usage exceeds 90%, also delete files in /var/log older than 30 days"
  - "restart nginx if it is not running"
  - "restart postgresql if it is not running"
  - "write an alert to /tmp/karya_high_mem.txt if memory usage exceeds 90%"
  - "log system metrics every cycle to /var/log/karya/metrics.csv"

constraints:
  - "never delete files in /home, /root, /etc, or /var/www"
  - "never run package managers: apt, yum, pip"
  - "never stop or disable systemd services — only restart"
  - "never delete database files or application data"
  - "if a command fails, log the error and do not retry more than once per hour"

cycle_interval_seconds: 60
dry_run: false
safe_gpio_pins: []

thresholds:
  - metric: disk_used_pct
    op: ">"
    value: 80
    check_every: 120    # check every 2 minutes, fire agent immediately on breach

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 5 — Dry run

```bash
karya run-once --dry-run --goals ~/karya/config/goals.yaml
```

You will see the priority ranking output. With a healthy disk, the log metrics goal should rank highest. If disk is above 80%, that goal jumps to URGENT.

### Step 6 — Run live

```bash
karya run-once --goals ~/karya/config/goals.yaml
```

Check `/var/log/karya/metrics.csv` to confirm logging is working.

### Step 7 — Check world state

```bash
karya status
```

This shows what karya knows — current disk %, memory %, temperature, and the last 5 actions taken.

### Step 8 — Start continuous monitoring

```bash
karya start --goals ~/karya/config/goals.yaml
```

### Step 9 — Run on boot with systemd

```bash
sudo nano /etc/systemd/system/karya.service
```

```ini
[Unit]
Description=karya — offline autonomous agent
After=multi-user.target ollama.service
Wants=multi-user.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/karya
ExecStart=/home/YOUR_USERNAME/.local/bin/karya start --goals /home/YOUR_USERNAME/karya/config/goals.yaml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=KARYA_HOME=/home/YOUR_USERNAME/.karya

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable karya
sudo systemctl start karya
sudo journalctl -u karya -f
```

---

## What karya does in practice

| Situation | karya's action |
|-----------|---------------|
| Disk at 82% | `journalctl --vacuum-size=200M`, `find /tmp -mtime +7 -delete` |
| Disk at 91% | Also runs `find /var/log -mtime +30 -name "*.log" -delete` |
| nginx stopped | `systemctl restart nginx`, logs result |
| Memory at 92% | Writes `/tmp/karya_high_mem.txt` with timestamp and current stats |
| All normal | Appends one line to `/var/log/karya/metrics.csv`, logs "no action needed" |

---

## Using the file watch trigger for ad-hoc commands

If you need karya to run a one-off task without SSHing into the machine, drop a task file:

```bash
# From any machine on the same network via scp, or locally
echo "run: df -h > /tmp/disk_report.txt" > ~/.karya/tasks/disk_report.txt
```

karya picks it up within 2 seconds and acts on it.

---

## Monitoring karya itself

```bash
# See recent activity
karya status

# Follow live decisions
sudo journalctl -u karya -f

# Check the metrics log
tail -20 /var/log/karya/metrics.csv

# Run a quick benchmark to see tokens/sec on your hardware
karya bench
```

---

## Troubleshooting

**karya keeps restarting the same service**
The service may be failing for a reason karya can't fix (e.g., a misconfiguration). Add to constraints: `"if nginx has been restarted more than 3 times in the last hour, write an alert to /tmp/karya_restart_loop.txt and do not restart again"`.

**karya doctor shows wrong tier**
On a VPS, available RAM may differ from total RAM. Add `model: "qwen2.5:1.5b"` explicitly in goals.yaml to override auto-detection.

**Disk log CSV is growing too large**
Add a goal: `"if /var/log/karya/metrics.csv exceeds 10MB, archive it and start a new file"`.
