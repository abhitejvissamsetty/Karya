# Use case 7 — predictive maintenance node

## What it does

A Raspberry Pi attached to factory equipment via an accelerometer on the serial port. karya continuously reads vibration data, compares it to a known good baseline, and detects when the pattern changes — a sign of bearing wear, imbalance, or imminent failure. When it detects an anomaly, it writes a structured maintenance alert. The maintenance team collects these on their next visit. No cloud, no continuous connectivity required.

---

## Who this is for

- Small manufacturers wanting predictive maintenance without expensive industrial IoT platforms
- Maintenance engineers in locations with poor connectivity
- Makers building low-cost condition monitoring systems

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi 4 (4GB) |
| Model | `qwen2.5:1.5b` |
| Sensor | ADXL345 3-axis accelerometer via I2C, or MPU-6050 |
| Interface | I2C (or SPI) on Pi GPIO |
| Connectivity | None required |

### Wiring (ADXL345 via I2C)

```
ADXL345 VCC   → Pi 3.3V
ADXL345 GND   → Pi GND
ADXL345 SDA   → Pi GPIO 2 (SDA)
ADXL345 SCL   → Pi GPIO 3 (SCL)
ADXL345 SDO   → GND (sets I2C address to 0x53)
```

Enable I2C: `raspi-config` → Interface Options → I2C → Enable.

---

## What you need before starting

- [ ] Raspberry Pi 4 with Raspberry Pi OS
- [ ] ADXL345 or MPU-6050 accelerometer wired via I2C
- [ ] `pip install adafruit-circuitpython-adxl34x` or equivalent
- [ ] Ollama installed and model pulled
- [ ] karya installed: `pip install karya`

---

## Step-by-step setup

### Step 1 — Test sensor reading

```python
# ~/karya/tools/read_vibration.py
import board, busio, json, time
import adafruit_adxl34x

i2c = busio.I2C(board.SCL, board.SDA)
sensor = adafruit_adxl34x.ADXL345(i2c)

# Read 10 samples and compute RMS
samples = []
for _ in range(10):
    x, y, z = sensor.acceleration
    samples.append((x**2 + y**2 + z**2) ** 0.5)
    time.sleep(0.05)

rms = (sum(s**2 for s in samples) / len(samples)) ** 0.5
peak = max(samples)

print(json.dumps({
    "rms_g": round(rms, 4),
    "peak_g": round(peak, 4),
    "samples": len(samples)
}))
```

```bash
python3 ~/karya/tools/read_vibration.py
# Expected output for healthy motor at idle: {"rms_g": 0.12, "peak_g": 0.18, "samples": 10}
```

### Step 2 — Establish a baseline

Run this on the equipment when it is known to be healthy. Record the RMS and peak values.

```bash
for i in {1..20}; do
  python3 ~/karya/tools/read_vibration.py
  sleep 5
done
```

Note the typical range. Example: RMS 0.08–0.15g, peak 0.12–0.22g when healthy.

### Step 3 — Create goals.yaml

Replace the threshold values with your actual baseline measurements.

```yaml
goals:
  - "read vibration sensor every cycle and log to /var/log/karya/vibration.csv"
  - "if RMS vibration exceeds 0.35g, write a WARNING alert to /var/log/karya/alerts/maintenance_alert.json"
  - "if RMS vibration exceeds 0.60g, write a CRITICAL alert — possible bearing failure imminent"
  - "if peak vibration exceeds 1.0g, write an EMERGENCY alert and log the raw reading immediately"
  - "if vibration has been elevated (above 0.30g) for more than 3 consecutive readings, escalate the alert severity"

constraints:
  - "always include timestamp, equipment_id, rms_g, peak_g, and severity in every alert file"
  - "never delete alert files"
  - "if sensor read fails, log the error and write a sensor_fault alert"
  - "equipment_id for this unit is MOTOR-LINE2-BEARING3"

cycle_interval_seconds: 60   # read every minute
dry_run: false
safe_gpio_pins: []

thresholds:
  - metric: cpu_temp_c
    op: ">"
    value: 80
    check_every: 120   # also alert if Pi itself overheats

ollama:
  base_url: "http://localhost:11434"
  model: "qwen2.5:1.5b"
```

### Step 4 — Create the vibration reading shell wrapper

karya calls tools as shell commands. Create the wrapper:

```bash
nano ~/karya/tools/vibration.sh
```

```bash
#!/bin/bash
python3 /home/pi/karya/tools/read_vibration.py 2>/dev/null
```

```bash
chmod +x ~/karya/tools/vibration.sh
```

### Step 5 — Dry run

```bash
karya run-once --dry-run --goals ~/karya/config/goals.yaml
```

Verify karya calls the vibration script, logs the reading, and correctly identifies normal vs anomalous levels.

### Step 6 — Start continuous monitoring

```bash
karya start --goals ~/karya/config/goals.yaml
```

### Step 7 — Enable on boot

Follow systemd setup from Use Case 3, Step 9.

---

## Alert file format

Every alert written to `/var/log/karya/alerts/`:

```json
{
  "timestamp": "2026-04-13T14:22:05",
  "equipment_id": "MOTOR-LINE2-BEARING3",
  "severity": "WARNING",
  "rms_g": 0.41,
  "peak_g": 0.68,
  "baseline_rms_g": 0.12,
  "deviation_factor": 3.4,
  "consecutive_elevated_readings": 2,
  "recommendation": "inspect bearing and lubrication at next scheduled maintenance window"
}
```

---

## Collecting alerts

```bash
# Copy alerts to USB on maintenance visit
cp /var/log/karya/alerts/*.json /media/usb/maintenance_$(date +%Y%m%d)/

# Or drop a task:
echo "copy /var/log/karya/alerts/ and /var/log/karya/vibration.csv to /media/usb/maintenance/" \
  > ~/.karya/tasks/collect.txt
```

---

## Understanding the vibration data

| RMS reading | Meaning | karya's response |
|-------------|---------|-----------------|
| 0.08–0.15g | Normal operating range | Log only |
| 0.15–0.35g | Slightly elevated — monitor | Log with note |
| 0.35–0.60g | Abnormal — schedule inspection | Write WARNING alert |
| 0.60–1.0g | Significant fault likely | Write CRITICAL alert |
| > 1.0g | Possible imminent failure | Write EMERGENCY alert |

Adjust these thresholds to match your specific equipment and baseline.

---

## Troubleshooting

**Readings are noisy and triggering false alerts**
Increase the number of samples in `read_vibration.py` from 10 to 50 and average over a longer window. Also check that the accelerometer is rigidly mounted — loose mounting amplifies vibration artificially.

**karya cannot find the I2C device**
Run `i2cdetect -y 1` to confirm the sensor is visible. Should show `53` at address 0x53. If not, check wiring and confirm I2C is enabled.

**Baseline varies with equipment speed/load**
You may need different thresholds for different operating modes. Add goals for each mode: `"if fan speed is HIGH and RMS exceeds 0.50g..."`.
