# Use case 4 — offline intruder response

## What it does

A Raspberry Pi connected to PIR motion sensors and a door sensor. When motion is detected, karya wakes up, reads all available sensor data, and decides whether this is a real intrusion or a false alarm — based on the time of day, recent event history, whether the door was opened first, and prior detections. It then activates a buzzer or siren via GPIO, logs the event, and writes a structured incident file. Zero cloud dependency.

---

## Who this is for

- Home owners wanting a smart alarm that reasons about context, not just raw motion
- Makers building a security system that works during internet outages
- Anyone who has been woken at 3am by a cat triggering a dumb motion sensor

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi 4 (4GB) |
| Model | `qwen2.5:1.5b` |
| Sensors | HC-SR501 PIR (GPIO 17), magnetic door sensor (GPIO 27) |
| Actuators | Active buzzer (GPIO 18), optional relay for siren |
| Power | Standard 5V power supply with UPS HAT for outage protection |

### Wiring

```
PIR sensor OUT   → GPIO pin 17  (input, pull-down)
Door sensor      → GPIO pin 27  (input, pull-up — LOW = door open)
Buzzer positive  → GPIO pin 18  (output)
Buzzer negative  → GND
```

---

## What you need before starting

- [ ] Raspberry Pi 4 with Raspberry Pi OS
- [ ] PIR sensor and door reed switch wired as above
- [ ] Buzzer or relay connected to GPIO 18
- [ ] Ollama installed, model pulled
- [ ] karya installed: `pip install karya[gpio]`

---

## Step-by-step setup

### Step 1 — Test your sensors manually

```bash
python3 << 'EOF'
import RPi.GPIO as GPIO, time
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)   # PIR
GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)     # door
GPIO.setup(18, GPIO.OUT)                               # buzzer

print("Monitoring sensors for 30 seconds. Move in front of PIR and open door.")
for _ in range(30):
    pir  = GPIO.input(17)
    door = GPIO.input(27)
    print(f"PIR: {'MOTION' if pir else 'clear'}  Door: {'OPEN' if not door else 'closed'}")
    time.sleep(1)
GPIO.cleanup()
EOF
```

Confirm sensors respond correctly before proceeding.

### Step 2 — Test the buzzer

```bash
python3 -c "
import RPi.GPIO as GPIO, time
GPIO.setmode(GPIO.BCM)
GPIO.setup(18, GPIO.OUT)
GPIO.output(18, GPIO.HIGH); time.sleep(1); GPIO.output(18, GPIO.LOW)
GPIO.cleanup()
print('buzzer test ok')
"
```

### Step 3 — Create goals.yaml

```yaml
goals:
  - "if PIR on pin 17 detects motion at night (22:00–06:00), activate buzzer on pin 18 for 10 seconds and log an incident"
  - "if PIR detects motion and door sensor on pin 27 was opened in the last 60 seconds, log as likely entry event"
  - "if PIR detects motion during daytime (06:00–22:00), log the event but do not activate buzzer"
  - "if motion is detected more than 3 times in 5 minutes, write a high-priority alert to /tmp/karya_intrusion_alert.txt"
  - "if no motion for 30 minutes after an alert, write a clear event to the incident log"

constraints:
  - "never activate buzzer during daytime (06:00–22:00) unless intrusion_override.txt exists in ~/.karya/tasks/"
  - "never activate buzzer more than once every 5 minutes for the same trigger"
  - "always log every motion event regardless of other decisions"
  - "write incident log to /var/log/karya/incidents.jsonl"

cycle_interval_seconds: 60   # background heartbeat
dry_run: false
safe_gpio_pins: [18]         # only buzzer pin allowed for writes

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 4 — Add a GPIO trigger for instant wake-up

The `cycle_interval_seconds` is just the background heartbeat. The real power is the GPIO trigger — karya wakes instantly when the PIR fires, instead of waiting up to 60 seconds.

Add to goals.yaml:

```yaml
gpio_triggers:
  - pin: 17
    edge: "rising"     # fires when PIR detects motion
    pull_up: false
```

Now karya responds within milliseconds of motion detection.

### Step 5 — Dry run at night time (or spoof the time)

```bash
karya run-once --dry-run --goals ~/karya/config/goals.yaml
```

Check that the priority ranking correctly identifies nighttime motion as URGENT and daytime motion as normal.

### Step 6 — Start karya

```bash
karya start --goals ~/karya/config/goals.yaml
```

### Step 7 — Autostart on boot

Follow the systemd setup from Use Case 3, Step 9. Add the `gpio_triggers` section to your service environment if needed.

---

## What karya does in practice

| Event | Time | karya's action |
|-------|------|---------------|
| PIR triggers | 02:30 | Activates buzzer 10s, writes incident to log |
| PIR + door opened | 02:35 | Logs "likely entry event", writes high-priority alert |
| PIR triggers 4 times in 4 min | 02:40 | Writes `/tmp/karya_intrusion_alert.txt` |
| PIR triggers | 14:00 | Logs event, no buzzer |
| No motion for 30 min | 03:10 | Logs "all clear" event |

---

## Reading incident logs

```bash
# View recent incidents
cat /var/log/karya/incidents.jsonl | python3 -m json.tool | tail -50

# Count incidents in last 24h
grep $(date +%Y-%m-%d) /var/log/karya/incidents.jsonl | wc -l
```

---

## Override from another device

To temporarily disable the buzzer (e.g., you arrive home late):

```bash
# From your laptop on the same network
ssh pi@raspberrypi.local "echo 'buzzer disabled until 07:00' > ~/.karya/tasks/intrusion_override.txt"
```

karya reads this file via the file watch trigger and adjusts its behaviour.

---

## Troubleshooting

**Too many false alarms from the PIR**
Add to constraints: `"only activate buzzer if PIR has been triggered more than once in a 2-minute window"`. Single brief triggers are often insects or temperature changes.

**Buzzer activates during the day**
Verify the time check in your goals. karya uses the system clock — confirm it is set correctly with `timedatectl`.

**karya misses a PIR trigger**
Check the GPIO trigger is configured correctly in goals.yaml and that `RPi.GPIO` is installed. Run `karya doctor` — it lists GPIO library availability.
