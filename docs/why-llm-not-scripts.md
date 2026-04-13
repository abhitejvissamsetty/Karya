# Why an LLM and not just cron + scripts?

This is the most important question about karya. It deserves a direct answer.

---

## The short answer

For simple, predictable failures — a full disk, a crashed service, a threshold breach — **you do not need karya**. A cron job and three lines of bash does that fine, runs faster, uses no RAM, and never hallucinates.

karya exists for everything else.

---

## What "everything else" actually means

### 1. Real failures are almost always combinations

A rule-based monitoring script has rules like this:

```bash
if disk_pct > 85; then clean_tmp; fi
if nginx_status == "inactive"; then systemctl restart nginx; fi
if temp_c > 75; then write_alert; fi
```

Each rule is written in isolation. They do not talk to each other. The script does not know that nginx went down *because* the disk is full, and cleaning the disk first would fix both problems without a restart. It just fires rules in sequence.

karya sees the full picture at once:

```
SYSTEM:  disk=91% | mem=88% | temp=77°C
FACTS:   nginx=down, last_restart=3min ago (failed)
GOALS:   keep disk <85%, restart nginx if down
HISTORY: restarted nginx twice in last 10 minutes — both failed
```

It reasons: nginx keeps failing and memory is critically high — the most likely cause is disk exhaustion preventing nginx from writing logs. Clean disk first. Then restart. The causal chain **disk full → nginx crash** is not in any individual rule. It emerges from reasoning across the whole state.

This is the difference. Real failures chain. Rules do not.

---

### 2. Rule-based systems do not know when to stop

A rule fires whenever its condition is true. It has no concept of "this action will make the situation worse."

Consider: disk is at 91%, but the system is mid-way through a database backup that is actively writing to that disk. A rule-based cleaner deletes temp files immediately. karya reads the running process list, sees the backup, reads the constraint `"never delete files during active database operations"`, and waits — even though the disk goal is technically breached.

Or: temperature is 76°C on a Pi in a greenhouse. A rule fires the cooling fan. karya checks the world state: humidity is already at 95% and the fan intake is exposed. Running the fan will pull in humid air and potentially damage the electronics. It logs the concern and instead opens a roof vent.

**Contextual restraint** — knowing when not to act — requires reading multiple signals simultaneously and applying judgment. Rules cannot do this without being manually programmed for every possible combination, which is not feasible as systems grow.

---

### 3. Goal conflict resolution

A greenhouse controller has these goals running simultaneously:

- Keep temperature above 18°C
- Keep humidity below 75%
- Keep CO2 above 800ppm
- Conserve power between midnight and 6am
- Do not run the heater and the vent fan at the same time

At 2am, temperature drops to 17°C. Consider the options:

- Turn on the heater — raises temperature, but drops CO2 and increases power use
- Turn on the vent fan — raises CO2, but drops temperature further
- Open the roof vent — raises CO2, equalises temperature, uses no power

A rule-based system either acts on the first matching rule (often wrong) or requires a manually written priority matrix covering every combination of conditions — which grows exponentially as goals are added and becomes impossible to maintain.

karya's priority ranker scores every goal against the current state, identifies the single most urgent one, and then the LLM reasons through the tradeoffs to find the action with the best net outcome across all active goals simultaneously.

---

### 4. Interpreting ambiguous sensor data

A vibration sensor reading of 0.4g is either:

- **Normal** — if the machine just started up two minutes ago
- **Abnormal** — if it has been running steadily for two hours at 0.12g
- **A sensor fault** — if it jumped from 0.1g to 0.4g in a single reading with no intermediate values

A threshold-based system fires an alert at 0.35g regardless of which of these three situations it is. karya reads the recent history in the world state, sees the machine started three minutes ago, and correctly classifies it as a startup transient rather than a fault.

Three false alerts avoided per day. One real fault still caught. Maintenance teams stop ignoring the alerts.

---

### 5. Instructions that cannot be pre-programmed

The file watch (dead drop) use case is entirely impossible without an LLM. Consider this task file dropped by a field engineer:

```
the backup from last Tuesday seems incomplete — check if the tar archive in /backups/
is a valid archive and if the file count is lower than previous weeks. if something
is wrong write a summary to /tmp/backup_report.txt
```

There is no rule you can write in advance for this. No script, because the script would have to be written specifically for this exact instruction before the engineer wrote it. karya parses the instruction, forms a plan (`tar -tvf`, compare against previous archives, check return codes), executes it in sequence, and writes a coherent summary.

This is the qualitative difference: **karya can act on instructions it has never seen before**. Rule engines cannot.

---

## Side-by-side comparison

| Scenario | cron + bash | karya |
|----------|-------------|-------|
| Disk above 85% → clean /tmp | ✅ Works perfectly | ✅ Works, slightly slower |
| nginx down → restart | ✅ Works perfectly | ✅ Works, slightly slower |
| nginx down *because* disk is full → fix root cause | ❌ Restarts nginx (fails again) | ✅ Cleans disk first, then restarts |
| Sensor reading: startup transient vs real fault | ❌ Cannot distinguish | ✅ Reads history, classifies correctly |
| Three competing goals with conflicting actions | ❌ First matching rule wins | ✅ Scores all goals, picks best net action |
| Novel instruction from field operator | ❌ Impossible | ✅ Parses and executes |
| Restraint: do not act during active backup | ❌ Acts anyway | ✅ Waits, then acts |
| Maintenance: add a new condition | ❌ Write and deploy new script | ✅ Add one line to goals.yaml |

---

## The honest costs

karya adds real costs that you should know about before choosing it.

| Cost | Reality |
|------|---------|
| **Latency** | Each decision takes 2–10 seconds on a Pi 4 depending on model. Unsuitable for real-time control loops. |
| **RAM** | The model uses 1–4GB depending on size. This is your biggest hardware constraint. |
| **Hallucination risk** | The LLM can propose wrong actions. The safety guard blocks the most dangerous ones, but you must write good constraints. |
| **First-load delay** | Model loading takes 30–60 seconds on a Pi Zero. After that it stays resident. |
| **Non-determinism** | Temperature is set low (0.1) but the same state may produce slightly different decisions on different runs. |

**When to use cron + bash instead of karya:**
- The failure mode is a single, well-understood condition
- You need sub-second response time
- You have less than 1.5GB RAM
- The system is safety-critical and every action must be deterministic

**When to use karya:**
- Failures are combinations of conditions that interact
- You cannot enumerate all possible situations in advance
- Instructions arrive as natural language from non-technical operators
- The system needs contextual restraint — knowing when not to act
- You want to add new behaviours by editing a text file, not deploying code

---

## The 80/20 of it

In most edge deployments, 80% of situations are routine — a threshold crossed, a service restarted, a log cleaned. Scripts handle these fine. karya handles these too, just slightly slower.

The remaining 20% are the novel combinations: two things failing together in a way no single rule covers, an ambiguous sensor reading, a conflicting goal, an instruction that was never anticipated. These are the situations that cause downtime, data loss, and equipment damage.

**karya is built for that 20%.** The 80% is just it earning its presence on the hardware.

---

## What a sysadmin would say

> "I can write bash scripts that handle all of this."

Yes. A skilled sysadmin can. And those scripts will handle the cases they were written for perfectly. They will also be wrong every time something happens that the sysadmin did not anticipate when writing them — which is exactly when you most need the system to reason correctly.

karya is not a replacement for sysadmin expertise. It is a system that applies that expertise continuously, without the sysadmin having to be present, and that handles the cases the scripts miss.

---

## A concrete before/after

### Before karya — rule-based monitoring

```bash
# check_system.sh — runs every 5 minutes via cron
#!/bin/bash

DISK=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK" -gt 85 ]; then
  find /tmp -mtime +7 -delete
  journalctl --vacuum-size=200M
fi

if ! systemctl is-active nginx > /dev/null; then
  systemctl restart nginx
fi

if ! systemctl is-active postgresql > /dev/null; then
  systemctl restart postgresql
fi
```

This script handles three specific cases it was written for.

It does not handle:
- nginx failing because postgresql is filling the disk with WAL logs
- postgresql crashing because nginx left stale lock files
- disk filling because the backup process is running and should not be interrupted
- temperature spiking because the fan failed three hours ago and the disk is now throttling
- a new service added last week that nobody updated the script for

Every one of these situations requires a human to investigate.

### After karya — goals.yaml

```yaml
goals:
  - "keep disk usage below 85% — clean /tmp and vacuum journals if needed"
  - "restart nginx if it stops — but check disk is healthy first"
  - "restart postgresql if it stops — but check for lock files in /tmp first"
  - "alert if temperature exceeds 75°C and log what processes are consuming most CPU"
  - "if disk usage is above 90% and a backup is running, wait for the backup to finish before cleaning"
  - "log system metrics every cycle to /var/log/karya/metrics.csv"

constraints:
  - "never delete database files, WAL logs, or application data"
  - "never restart postgresql and nginx at the same time"
  - "if the same service has been restarted more than 3 times in one hour, write an alert instead of restarting again"
```

This configuration handles every case the script handles, plus every case the script misses. Adding a new service is one line. Adding a new constraint is one line. No deployment, no testing, no code review.

---

## Summary

karya does not replace scripts for simple cases. It handles the hard cases that scripts cannot: novel failure combinations, conflicting goals, contextual restraint, and natural language instructions. The LLM is not doing magic — it is doing what a thoughtful on-call engineer does: reading the full situation, considering consequences, and choosing the action most likely to produce a good outcome across all goals simultaneously.

The difference is that it does this at 3am, in a field with no signal, on a £35 computer, without waking anyone up.
