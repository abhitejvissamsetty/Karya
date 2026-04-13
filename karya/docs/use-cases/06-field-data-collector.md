# Use case 6 — field data collector

## What it does

A Raspberry Pi carried into the field — a mine, a forest, an offshore platform, a construction site — where there is no mobile signal. A researcher or technician writes a task as plain text onto a USB drive or drops it into a folder. karya reads it, executes the instructions (run analysis scripts, compress logs, reorganise files, generate reports), and writes the results back. No terminal, no SSH, no internet required.

This is karya's "dead drop" interface: instructions in, results out, via files.

---

## Who this is for

- Field engineers and technicians who need to run ad-hoc commands on a deployed device
- Researchers processing data collected by sensors before returning to the lab
- Operations teams managing devices in RF-restricted environments (mines, offshore, server vaults)

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi 4 (4GB) in a ruggedised case |
| Model | `qwen2.5:1.5b` |
| Interface | USB drive for task/result file exchange |
| Connectivity | None required |

---

## What you need before starting

- [ ] Raspberry Pi 4 with Raspberry Pi OS
- [ ] Ollama installed and model pulled
- [ ] karya installed: `pip install karya`
- [ ] USB drive for task delivery

---

## How the file watch trigger works

karya watches `~/.karya/tasks/` for new `.txt` files. When one appears:

1. karya reads the file contents as the task description
2. It decides what action to take and executes it
3. It moves the task file to `~/.karya/tasks/done/`
4. Results are written to `/var/log/karya/` or wherever the task specifies

You deliver tasks by dropping a `.txt` file into that directory — via USB mount, direct write, or any means available on-site.

---

## Step-by-step setup

### Step 1 — Install and start karya

```bash
pip install karya
OLLAMA_CONTEXT_LENGTH=4096 ollama serve &
ollama pull qwen2.5:1.5b
```

### Step 2 — Create goals.yaml

The goals define what kinds of tasks karya is allowed to execute. Be specific about what is permitted.

```yaml
goals:
  - "when a task file arrives in ~/.karya/tasks/, read it and execute the requested operation"
  - "after completing a task, write a result summary to /var/log/karya/results/TASKNAME_result.txt"
  - "if a task requests data export, copy the requested files to /media/usb/ if a USB drive is mounted"
  - "if a task requests a system report, run diagnostics and write to /var/log/karya/reports/"
  - "log all task executions with timestamp and outcome to /var/log/karya/task_log.jsonl"

constraints:
  - "never execute tasks that delete files outside /tmp or /var/log/karya"
  - "never execute tasks that install software"
  - "never transmit data over any network"
  - "if a task instruction is ambiguous, write a clarification request to the result file and stop"
  - "maximum one task file processed per cycle — do not batch"

cycle_interval_seconds: 3600   # background health check only
dry_run: false
safe_gpio_pins: []

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 3 — Mount USB auto-detection (optional)

For automatic USB task delivery, add a udev rule:

```bash
sudo nano /etc/udev/rules.d/99-karya-usb.rules
```

```
ACTION=="add", KERNEL=="sd[a-z]1", RUN+="/bin/bash -c 'mount /dev/%k /media/usb && cp /media/usb/tasks/*.txt /home/pi/.karya/tasks/ 2>/dev/null'"
```

```bash
sudo udevadm control --reload-rules
```

Now when you plug in a USB drive with a `tasks/` folder, karya picks up any `.txt` files automatically.

### Step 4 — Dry run with a test task

Create a test task:

```bash
echo "generate a system health report including disk usage, memory, CPU temperature, and uptime. Save it to /var/log/karya/reports/health_$(date +%Y%m%d).txt" \
  > ~/.karya/tasks/health_check.txt
```

Dry run:

```bash
karya run-once --dry-run --goals ~/karya/config/goals.yaml
```

Confirm karya reads the task, proposes appropriate shell commands, and the result path is correct.

### Step 5 — Live run

```bash
karya run-once --goals ~/karya/config/goals.yaml
cat /var/log/karya/reports/health_*.txt
```

### Step 6 — Start the autonomous loop

```bash
karya start --goals ~/karya/config/goals.yaml
```

From this point on, any `.txt` file dropped into `~/.karya/tasks/` will be processed within seconds.

---

## Writing good task files

karya reads the entire file as the task description. Write clearly, as if instructing a careful technician.

**Good task file:**
```
compress all .csv files in /var/log/sensors/ that are older than 7 days into a single archive at /var/log/sensors/archive_april2026.tar.gz, then delete the original .csv files
```

**Also good:**
```
generate a report of the 10 largest files in /var/log/ and save it to /tmp/large_files_report.txt
```

**Too vague (karya will ask for clarification):**
```
clean up the logs
```

**Not permitted (blocked by constraints):**
```
install python-pandas using pip
```

---

## Example task files for common field operations

**Daily data export to USB:**
```
copy all files in /var/log/karya/captures/ modified today to /media/usb/export_$(date +%Y%m%d)/
```

**Sensor data compression:**
```
find /var/log/sensors/ -name "*.csv" -mtime +3 -exec gzip {} \; and report how many files were compressed
```

**System diagnostics:**
```
run: df -h, free -h, uptime, vcgencmd measure_temp — combine all output into /tmp/diagnostics.txt
```

**Log archive:**
```
tar -czf /media/usb/logs_backup.tar.gz /var/log/karya/ and write the archive size to /tmp/backup_result.txt
```

---

## Collecting results

After karya processes a task:

- Task file is moved to `~/.karya/tasks/done/TASKNAME.txt`
- Result is written to `/var/log/karya/results/TASKNAME_result.txt`
- Full log entry added to `/var/log/karya/task_log.jsonl`

To collect on a USB drive, either read directly or drop a collection task:

```bash
echo "copy /var/log/karya/results/ to /media/usb/results/" > ~/.karya/tasks/collect_results.txt
```

---

## Troubleshooting

**karya says task is ambiguous**
Rewrite the task with a specific file path, command, or outcome. karya will not guess at ambiguous instructions — this is by design.

**USB drive not detected**
Check `dmesg | tail -10` after plugging in. Ensure `/media/usb` exists: `sudo mkdir -p /media/usb`.

**Task processed but result file missing**
Check `/var/log/karya/task_log.jsonl` for the error. The most common cause is a path that doesn't exist — ensure the target directory is created first.
