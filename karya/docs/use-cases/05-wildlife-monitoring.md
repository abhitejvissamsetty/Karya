# Use case 5 — wildlife monitoring station

## What it does

A solar-powered Raspberry Pi Zero 2W deployed in a field, forest, or coastline for months at a time. When a PIR sensor detects movement, karya wakes, logs a timestamped structured event, triggers a camera module to capture an image, classifies the event type based on prior history, and writes everything to SD card. The researcher visits monthly to collect data — no internet ever touches the device.

---

## Who this is for

- Ecologists and field researchers running wildlife surveys
- Conservation organisations monitoring protected sites
- Citizen scientists building low-cost camera traps

---

## Hardware

| Item | Spec |
|------|------|
| Device | Raspberry Pi Zero 2W (512MB RAM) |
| Model | `tinyllama:1.1b` Q4_K_M via llama.cpp |
| Sensors | HC-SR501 PIR (GPIO 17) |
| Camera | Raspberry Pi Camera Module 3 |
| Power | 10W solar panel + 10,000mAh LiPo battery + Waveshare Solar HAT |
| Storage | 128GB microSD |
| Housing | Weatherproof IP65 enclosure |

---

## Why llama.cpp instead of Ollama on Pi Zero

The Pi Zero 2W has only 512MB RAM. Ollama's overhead alone uses ~200MB before the model loads. Using `llama-server` from llama.cpp directly saves ~150MB and gives more headroom for the 1.1B model.

---

## What you need before starting

- [ ] Pi Zero 2W with Raspberry Pi OS Lite (64-bit, headless)
- [ ] Camera module connected and enabled (`raspi-config` → Interface Options → Camera)
- [ ] PIR sensor wired to GPIO 17
- [ ] llama.cpp compiled for ARM: `make pi` in the llama.cpp repo
- [ ] TinyLlama 1.1B GGUF model downloaded
- [ ] karya installed: `pip install karya[gpio]`

---

## Step-by-step setup

### Step 1 — Compile llama-server for Pi Zero

On the Pi Zero (or cross-compile):

```bash
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
make -j4 llama-server
```

Download the model:

```bash
pip install huggingface-hub
huggingface-cli download \
  bartowski/TinyLlama-1.1B-Chat-v1.0-GGUF \
  TinyLlama-1.1B-Chat-v1.0-Q4_K_M.gguf \
  --local-dir ~/models/
```

Start llama-server:

```bash
~/llama.cpp/llama-server \
  -m ~/models/TinyLlama-1.1B-Chat-v1.0-Q4_K_M.gguf \
  -c 512 \
  --port 8080 \
  --host 127.0.0.1 \
  -t 4 \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 &
```

### Step 2 — Test the camera

```bash
libcamera-still -o /tmp/test.jpg --timeout 2000
ls -lh /tmp/test.jpg
```

### Step 3 — Create a camera capture script

karya triggers this script via a shell tool call:

```bash
nano ~/karya/tools/capture.sh
```

```bash
#!/bin/bash
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT="/var/log/karya/captures/${TIMESTAMP}.jpg"
mkdir -p /var/log/karya/captures
libcamera-still -o "$OUTPUT" --timeout 1000 --nopreview 2>/dev/null
echo "captured: $OUTPUT"
```

```bash
chmod +x ~/karya/tools/capture.sh
```

### Step 4 — Create goals.yaml

```yaml
goals:
  - "when triggered by PIR motion on pin 17, run the camera capture script and log the event"
  - "write a structured JSON event record to /var/log/karya/events.jsonl with timestamp, event type, and capture filename"
  - "if more than 5 captures happen in 10 minutes, log a high-activity event — possible large animal or human"
  - "if SD card usage exceeds 80%, stop capturing and write a storage_full alert to /var/log/karya/alerts.txt"
  - "log battery voltage every hour if available via serial"

constraints:
  - "never delete captured images"
  - "never transmit data over any network"
  - "keep cycle time under 30 seconds to preserve battery"
  - "if camera capture fails, log the error and continue — do not retry immediately"

cycle_interval_seconds: 3600   # hourly heartbeat for health checks
dry_run: false
safe_gpio_pins: []             # no GPIO writes needed — PIR is input only

ollama:
  base_url: "http://localhost:8080"    # points to llama-server, not Ollama
  model: "tinyllama"

gpio_triggers:
  - pin: 17
    edge: "rising"
    pull_up: false
    debounce_ms: 500

ollama:
  base_url: "http://localhost:8080"
  model: ""
```

### Step 5 — Switch to the llama.cpp backend

```bash
karya start --goals ~/karya/config/goals.yaml --backend llamacpp --base-url http://localhost:8080
```

### Step 6 — Verify a full capture cycle

Trigger the PIR manually (wave your hand) and confirm:

```bash
ls /var/log/karya/captures/     # new .jpg file should appear
tail -1 /var/log/karya/events.jsonl   # structured event record
```

### Step 7 — Set up autostart for headless deployment

```bash
sudo nano /etc/systemd/system/llama-server.service
```

```ini
[Unit]
Description=llama-server for karya
After=multi-user.target

[Service]
Type=simple
User=pi
ExecStart=/home/pi/llama.cpp/llama-server -m /home/pi/models/TinyLlama-1.1B-Chat-v1.0-Q4_K_M.gguf -c 512 --port 8080 --host 127.0.0.1 -t 4 --cache-type-k q4_0 --cache-type-v q4_0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable llama-server karya
sudo systemctl start llama-server
sleep 10   # wait for model to load
sudo systemctl start karya
```

### Step 8 — Deploy in the field

Before sealing the enclosure:

```bash
# Confirm both services running
sudo systemctl status llama-server karya

# Confirm SD card has enough space
df -h /

# Confirm time is set (important for event timestamps)
timedatectl
# If no internet: manually set time
sudo timedatectl set-time "2026-04-13 08:00:00"

# Final dry run
karya run-once --dry-run --backend llamacpp --base-url http://localhost:8080
```

Seal, mount at camera height (typically 50–80cm for ground animals, 1.5m for larger mammals).

---

## What karya logs per event

Each event in `events.jsonl`:

```json
{
  "timestamp": "2026-05-01T03:42:17",
  "trigger": "gpio:pin17 rising edge",
  "event_type": "motion_detected",
  "capture": "/var/log/karya/captures/20260501_034217.jpg",
  "cycle": 847,
  "notes": "second detection in 3 minutes — possible repeat visit"
}
```

---

## Collecting data (monthly visit)

```bash
# Mount USB drive and copy everything
sudo mount /dev/sda1 /mnt/usb
cp -r /var/log/karya/ /mnt/usb/karya_data_$(date +%Y%m)/
sudo umount /mnt/usb

# Or use the file watch trigger remotely:
echo "copy /var/log/karya to /mnt/usb/" > ~/.karya/tasks/export.txt
```

---

## Troubleshooting

**Model takes too long to load on Pi Zero**
This is expected — first load takes 30–60 seconds. After that, responses are 2–4 tokens/sec. The GPIO trigger ensures karya is always ready to act once loaded.

**Camera captures are blurry**
Add `--autofocus-mode auto` to the libcamera-still command if using Camera Module 3.

**Battery draining too fast**
Increase `cycle_interval_seconds` to 7200 (2h). The GPIO trigger still fires instantly on motion — the long interval only affects the hourly health check.
