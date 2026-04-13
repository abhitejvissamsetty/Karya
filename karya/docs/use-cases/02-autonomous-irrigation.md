# Use case 2 — autonomous irrigation brain

## What it does

A Raspberry Pi deployed in a field with no mobile signal. It reads soil moisture, air temperature, and light level sensors every 15 minutes and decides when to open or close irrigation valves connected via GPIO relays. Once deployed, it runs for weeks without any human interaction.

---

## Who this is for

- Smallholder farmers wanting to automate irrigation without a cloud subscription
- Hobbyist gardeners with a greenhouse or allotment
- Agricultural researchers running field trials in remote locations

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi 4 (4GB) |
| Model | `qwen2.5:1.5b` |
| Sensors | Capacitive soil moisture sensor (GPIO ADC), DHT22 temperature/humidity, LDR light sensor |
| Actuators | 4-channel relay module connected to solenoid valves |
| Power | 12V solar panel + battery + 5V step-down converter |
| Connectivity | None required after setup |

### Wiring overview

```
Soil sensor  → ADC (MCP3008) → SPI → Pi GPIO
DHT22        → GPIO pin 4
Relay IN1    → GPIO pin 18   (valve zone 1)
Relay IN2    → GPIO pin 23   (valve zone 2)
Relay IN3    → GPIO pin 24   (valve zone 3)
```

---

## What you need before starting

- [ ] Raspberry Pi 4 running Raspberry Pi OS (64-bit)
- [ ] Soil moisture sensor wired to GPIO via MCP3008 ADC or I2C equivalent
- [ ] Relay module wired to GPIO pins
- [ ] Solenoid valve or water pump connected through relay
- [ ] Ollama installed and model pulled
- [ ] karya installed
- [ ] `RPi.GPIO` installed: `pip install RPi.GPIO`

---

## Step-by-step setup

### Step 1 — Install and verify hardware

```bash
# Test relay manually before letting karya control it
python3 -c "
import RPi.GPIO as GPIO, time
GPIO.setmode(GPIO.BCM)
GPIO.setup(18, GPIO.OUT)
GPIO.output(18, GPIO.HIGH)   # open valve
time.sleep(3)
GPIO.output(18, GPIO.LOW)    # close valve
GPIO.cleanup()
print('relay test ok')
"
```

### Step 2 — Install karya with GPIO support

```bash
pip install karya[gpio]
```

### Step 3 — Write a sensor reading script

karya uses shell commands to read sensors. Create a script it can call:

```bash
nano ~/karya/tools/read_sensors.sh
```

```bash
#!/bin/bash
# Returns JSON: {"moisture": 42, "temp_c": 24.1, "light": 680}
python3 ~/karya/tools/read_sensors.py
```

```bash
chmod +x ~/karya/tools/read_sensors.sh
```

```python
# ~/karya/tools/read_sensors.py
import json, random  # replace with your actual sensor library

# Example with real sensors (replace with your library):
# import board, adafruit_dht, busio, adafruit_mcp3xxx.mcp3008 as MCP
# ... read actual values

data = {
    "moisture_pct": 42,    # 0-100, higher = wetter
    "temp_c": 24.1,
    "light_lux": 680,
}
print(json.dumps(data))
```

### Step 4 — Create goals.yaml

```yaml
goals:
  - "if soil moisture drops below 35%, open irrigation valve on GPIO pin 18 for 10 minutes"
  - "if soil moisture is above 70%, ensure all valves are closed"
  - "if temperature exceeds 38°C, open misting valve on GPIO pin 23 for 5 minutes"
  - "log sensor readings every cycle to /var/log/karya/irrigation.csv"
  - "if light level drops below 100 lux (sunset), close all valves and stop irrigation"

constraints:
  - "never open more than one valve at a time"
  - "never irrigate between 23:00 and 05:00"
  - "if sensor reading fails, close all valves and log the error"
  - "if any valve has been open for more than 20 minutes, close it"

cycle_interval_seconds: 300   # check every 5 minutes
dry_run: false
safe_gpio_pins: [18, 23, 24]  # only these pins allowed for writes

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 5 — Add a threshold trigger for fast response

For immediate response when soil gets very dry, add a threshold trigger. Edit `goals.yaml`:

```yaml
thresholds:
  - metric: disk_used_pct    # placeholder — replace with custom sensor metric
    op: ">"
    value: 85
    check_every: 60
```

For a custom soil moisture threshold, karya can use the file watch trigger — your sensor script writes to a file when moisture drops critically low, and karya wakes immediately.

### Step 6 — Dry run and verify

```bash
karya run-once --dry-run --goals ~/karya/config/goals.yaml
```

Check that karya reads the sensors correctly and proposes the right valve actions before letting it act.

### Step 7 — Start autonomous operation

```bash
karya start --goals ~/karya/config/goals.yaml
```

### Step 8 — Deploy in the field

For field deployment without a screen, set up systemd autostart (same as Use Case 1, Step 8). The system will start automatically on power-up with no keyboard or monitor needed.

---

## What karya does in practice

| Sensor reading | karya's action |
|---------------|---------------|
| Moisture 32%, daytime | Opens valve on pin 18 for 10 min, logs event |
| Moisture 75% | Confirms all valves closed, logs "no action" |
| Temp 39°C | Opens misting valve on pin 23 for 5 min |
| Light 80 lux (dusk) | Closes all valves, logs "irrigation halted for night" |
| Sensor read fails | Closes all valves immediately, writes error to log |

---

## Reading the logs remotely

When you visit the field, mount the Pi over SSH or pull the log file:

```bash
# From your laptop when on same network
scp pi@raspberrypi.local:/var/log/karya/irrigation.csv ./
```

Or drop a task file via USB to request a log export:

```bash
echo "copy /var/log/karya/irrigation.csv to /media/usb/irrigation_export.csv" \
  > ~/.karya/tasks/export.txt
```

---

## Troubleshooting

**Valve opened but never closed**
Add to constraints: `"if any valve has been open for more than 20 minutes, close it"`. This acts as a failsafe.

**karya makes a decision but sensor data is stale**
Ensure your sensor script actually reads hardware — not cached values. Test it manually: `bash ~/karya/tools/read_sensors.sh`.

**Pi consumes too much power on solar**
Increase `cycle_interval_seconds` to 600 (10 min) and reduce `ollama num_predict` in the backend config. The Pi can enter a low-power state between cycles.
