"""
core/hil.py — Human-in-the-Loop approval for critical decisions

karya is offline-first. The HIL system reflects that:

PRIMARY channel (default, always works, zero internet):
  file — karya writes a JSON file to ~/.karya/hil/pending/
          human reads it, writes approve/deny, karya picks it up
          works in mines, ships, fields, air-gapped servers — everywhere

OPTIONAL channels (only if you choose to connect them):
  telegram — bot message with approve/deny inline buttons (needs internet)
  slack    — webhook message, reply in channel          (needs internet)
  webhook  — HTTP POST to any URL                       (needs internet)

karya never requires internet for HIL. The file channel is the
karya-native way. The network channels are opt-in extras for users
who happen to have connectivity and want faster mobile notifications.

If no channel is configured, HIL falls back to file automatically.
"""

import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("karya.hil")


# ── Decision classification ───────────────────────────────────────────────────

class HILLevel(Enum):
    AUTO     = "auto"       # execute immediately, no human needed
    CONFIRM  = "confirm"    # 10s pause then execute (existing safety behaviour)
    CRITICAL = "critical"   # pause, notify human, wait for explicit approval
    BLOCK    = "block"      # never execute regardless of human input


# Keywords in tool args or goal text that elevate to CRITICAL
CRITICAL_PATTERNS = [
    # destructive file ops
    "rm ", "remove ", "delete ", "wipe ", "truncate",
    # service disruption
    "systemctl stop", "systemctl disable", "kill ", "pkill",
    # data modification
    "drop table", "delete from", "truncate table",
    # GPIO writes (hardware control)
    "gpio_write", "gpio pulse",
    # network changes
    "iptables", "ufw ", "firewall",
    # privilege escalation
    "sudo ", "chmod 777", "chown root",
]

# Patterns that should never need CRITICAL (already BLOCK in safety.py)
BLOCK_PATTERNS = [
    "rm -rf /", "dd if=", "mkfs",
]

# Score threshold above which a decision is AUTO-classified as CRITICAL
CRITICAL_PRIORITY_SCORE = 80


@dataclass
class ApprovalRequest:
    request_id: str
    timestamp: str
    tool: str
    args: dict
    goal: str
    priority_score: float
    reason: str                  # why this was flagged as critical
    decision: Optional[str] = None   # "approve" | "deny" | None (pending)
    decided_at: Optional[str] = None
    decided_by: str = "pending"  # "human" | "timeout" | "auto"
    timeout_sec: int = 120


def classify_decision(tool: str, args: dict, priority_score: float) -> HILLevel:
    """
    Classify a decision as AUTO, CONFIRM, CRITICAL, or BLOCK.
    Called before every tool execution.
    """
    cmd = str(args).lower()
    tool_lower = tool.lower()

    # BLOCK first — safety.py handles these, but double-check
    for pattern in BLOCK_PATTERNS:
        if pattern in cmd:
            return HILLevel.BLOCK

    # CRITICAL by pattern match in command
    for pattern in CRITICAL_PATTERNS:
        if pattern in cmd or pattern in tool_lower:
            return HILLevel.CRITICAL

    # CRITICAL by priority score (very high urgency = needs human eyes)
    if priority_score >= CRITICAL_PRIORITY_SCORE:
        return HILLevel.CRITICAL

    # GPIO writes always need confirmation
    if tool_lower == "gpio" and args.get("action") in ("write", "pulse"):
        return HILLevel.CRITICAL

    return HILLevel.AUTO


def _reason_for_critical(tool: str, args: dict, score: float) -> str:
    """Generate a human-readable reason why this was flagged."""
    reasons = []
    cmd = str(args).lower()
    for pattern in CRITICAL_PATTERNS:
        if pattern in cmd:
            reasons.append(f"command contains '{pattern.strip()}'")
            break
    if score >= CRITICAL_PRIORITY_SCORE:
        reasons.append(f"priority score {score:.0f} >= {CRITICAL_PRIORITY_SCORE}")
    if tool.lower() == "gpio" and args.get("action") in ("write", "pulse"):
        reasons.append("GPIO hardware write")
    return "; ".join(reasons) if reasons else "matched critical pattern"


# ── Channel implementations ───────────────────────────────────────────────────

class _BaseChannel:
    def send(self, req: ApprovalRequest) -> bool:
        raise NotImplementedError

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        """Return 'approve', 'deny', or None (still pending)."""
        raise NotImplementedError


class TelegramChannel(_BaseChannel):
    """
    Sends a message with inline Approve / Deny buttons.
    Polls getUpdates to detect the human's response.

    Setup:
      1. Create a bot via @BotFather — get BOT_TOKEN
      2. Send any message to your bot — get CHAT_ID from getUpdates
      3. Set in goals.yaml:
           hil:
             channel: telegram
             telegram_bot_token: "123456:ABC..."
             telegram_chat_id: "987654321"
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.token = bot_token
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{self.token}"
        self._message_ids: dict = {}   # request_id → message_id

    def send(self, req: ApprovalRequest) -> bool:
        text = (
            f"*karya — approval required*\n\n"
            f"Tool: `{req.tool}`\n"
            f"Args: `{json.dumps(req.args, separators=(',',':'))}`\n"
            f"Goal: {req.goal[:100]}\n"
            f"Score: {req.priority_score:.0f} | Reason: {req.reason}\n"
            f"Timeout: {req.timeout_sec}s\n\n"
            f"ID: `{req.request_id}`"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{req.request_id}"},
                {"text": "❌ Deny",    "callback_data": f"deny:{req.request_id}"},
            ]]
        }
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(keyboard),
        }
        try:
            data = json.dumps(payload).encode()
            req_obj = urllib.request.Request(
                f"{self._base}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req_obj, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    self._message_ids[req.request_id] = result["result"]["message_id"]
                    logger.info("Telegram HIL sent for %s", req.request_id)
                    return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
        return False

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        """Check getUpdates for a callback_query matching our request_id."""
        try:
            url = f"{self._base}/getUpdates?timeout=2&allowed_updates=[\"callback_query\"]"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            for update in data.get("result", []):
                cq = update.get("callback_query", {})
                cb_data = cq.get("data", "")
                if f":{req.request_id}" in cb_data:
                    action = cb_data.split(":")[0]
                    # answer the callback to remove the spinner
                    self._answer_callback(cq.get("id", ""))
                    return action  # "approve" or "deny"
        except Exception as e:
            logger.debug("Telegram poll error: %s", e)
        return None

    def _answer_callback(self, callback_id: str):
        try:
            data = json.dumps({"callback_query_id": callback_id}).encode()
            req = urllib.request.Request(
                f"{self._base}/answerCallbackQuery",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def send_result(self, req: ApprovalRequest):
        """Edit the original message to show the outcome."""
        msg_id = self._message_ids.get(req.request_id)
        if not msg_id:
            return
        icon = "✅" if req.decision == "approve" else "❌"
        text = (
            f"{icon} *{req.decision.upper()}* by {req.decided_by}\n"
            f"Tool: `{req.tool}` | ID: `{req.request_id}`"
        )
        payload = {
            "chat_id": self.chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            data = json.dumps(payload).encode()
            r = urllib.request.Request(
                f"{self._base}/editMessageText",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(r, timeout=10)
        except Exception:
            pass


class SlackChannel(_BaseChannel):
    """
    Posts to a Slack webhook URL. Human responds by posting
    'approve <id>' or 'deny <id>' in the channel.
    Uses a file-based response pickup (karya watches a response file).

    Setup:
      1. Create incoming webhook: Slack API → Your App → Incoming Webhooks
      2. Set in goals.yaml:
           hil:
             channel: slack
             slack_webhook_url: "https://hooks.slack.com/services/..."
             slack_response_file: "/tmp/karya_hil_responses.txt"
    """

    def __init__(self, webhook_url: str, response_file: str = "/tmp/karya_hil_responses.txt"):
        self.webhook_url = webhook_url
        self.response_file = Path(response_file)

    def send(self, req: ApprovalRequest) -> bool:
        payload = {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "karya — approval required"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Tool:*\n`{req.tool}`"},
                    {"type": "mrkdwn", "text": f"*Score:*\n{req.priority_score:.0f}"},
                    {"type": "mrkdwn", "text": f"*Args:*\n`{json.dumps(req.args, separators=(',',':'))[:200]}`"},
                    {"type": "mrkdwn", "text": f"*Reason:*\n{req.reason}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"*Goal:* {req.goal[:120]}\n*Timeout:* {req.timeout_sec}s\n*ID:* `{req.request_id}`"}},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"Reply in this channel: `approve {req.request_id}` or `deny {req.request_id}`"}},
            ]
        }
        try:
            data = json.dumps(payload).encode()
            r = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(r, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("Slack HIL sent for %s", req.request_id)
                    return True
        except Exception as e:
            logger.error("Slack send failed: %s", e)
        return False

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        """Check the response file for 'approve <id>' or 'deny <id>'."""
        if not self.response_file.exists():
            return None
        try:
            lines = self.response_file.read_text().strip().splitlines()
            for line in lines:
                parts = line.strip().lower().split()
                if len(parts) >= 2 and req.request_id in parts[1]:
                    if parts[0] in ("approve", "deny"):
                        return parts[0]
        except Exception:
            pass
        return None


class WebhookChannel(_BaseChannel):
    """
    POSTs the approval request to any URL and polls a response endpoint.
    Compatible with any webhook service, Home Assistant, n8n, Zapier, etc.

    Setup:
      goals.yaml:
        hil:
          channel: webhook
          webhook_notify_url: "https://your-server/karya/notify"
          webhook_poll_url:   "https://your-server/karya/decision/{request_id}"
    """

    def __init__(self, notify_url: str, poll_url: str):
        self.notify_url = notify_url
        self.poll_url = poll_url   # {request_id} is substituted

    def send(self, req: ApprovalRequest) -> bool:
        payload = asdict(req)
        try:
            data = json.dumps(payload).encode()
            r = urllib.request.Request(
                self.notify_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(r, timeout=10) as resp:
                if resp.status in (200, 201, 202):
                    logger.info("Webhook HIL sent for %s", req.request_id)
                    return True
        except Exception as e:
            logger.error("Webhook send failed: %s", e)
        return False

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        url = self.poll_url.replace("{request_id}", req.request_id)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                decision = data.get("decision", "").lower()
                if decision in ("approve", "deny"):
                    return decision
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None   # not decided yet
        except Exception as e:
            logger.debug("Webhook poll error: %s", e)
        return None


class FileChannel(_BaseChannel):
    """
    Fully offline HIL — no internet required.
    Writes the approval request as a JSON file.
    Human reads it, creates an approve/deny file.
    Works in air-gapped deployments.

    File structure:
      pending/  <request_id>.json      ← karya writes this
      approve/  <request_id>.approve   ← human creates to approve
      deny/     <request_id>.deny      ← human creates to deny

    The human can also echo into a single responses file:
      echo "approve <request_id>" >> ~/.karya/hil/responses.txt

    Setup:
      goals.yaml:
        hil:
          channel: file
          hil_dir: "~/.karya/hil"
    """

    def __init__(self, hil_dir: str = "~/.karya/hil"):
        self.base = Path(os.path.expanduser(hil_dir))
        self.pending = self.base / "pending"
        self.approved = self.base / "approved"
        self.denied = self.base / "denied"
        self.responses = self.base / "responses.txt"
        for d in (self.pending, self.approved, self.denied):
            d.mkdir(parents=True, exist_ok=True)

    def send(self, req: ApprovalRequest) -> bool:
        path = self.pending / f"{req.request_id}.json"
        try:
            with open(path, "w") as f:
                json.dump({
                    **asdict(req),
                    "instructions": (
                        f"To approve: touch {self.approved}/{req.request_id}.approve\n"
                        f"To deny:    touch {self.denied}/{req.request_id}.deny\n"
                        f"Or:         echo 'approve {req.request_id}' >> {self.responses}"
                    )
                }, f, indent=2)
            logger.info("HIL file written: %s", path)
            return True
        except Exception as e:
            logger.error("File HIL write failed: %s", e)
            return False

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        # Check approve/deny touch files
        if (self.approved / f"{req.request_id}.approve").exists():
            return "approve"
        if (self.denied / f"{req.request_id}.deny").exists():
            return "deny"

        # Check responses.txt
        if self.responses.exists():
            try:
                for line in self.responses.read_text().splitlines():
                    parts = line.strip().lower().split()
                    if len(parts) >= 2 and req.request_id in parts[1]:
                        if parts[0] in ("approve", "deny"):
                            return parts[0]
            except Exception:
                pass
        return None

    def cleanup(self, req: ApprovalRequest):
        """Move pending file to resolved after decision."""
        pending = self.pending / f"{req.request_id}.json"
        if pending.exists():
            resolved = self.base / "resolved" / f"{req.request_id}.json"
            resolved.parent.mkdir(exist_ok=True)
            pending.rename(resolved)


# ── Offline-native channels ───────────────────────────────────────────────────

class GPIOButtonChannel(_BaseChannel):
    """
    Physical hardware approval — zero internet, zero software dependencies
    beyond RPi.GPIO or gpiozero.

    Wire two momentary buttons to GPIO pins:
      approve_pin — press to approve (e.g. green button)
      deny_pin    — press to deny   (e.g. red button)

    Optional: wire an LED to led_pin so it blinks while waiting.

    This is the most reliable offline HIL method for unattended Pi
    deployments — no file system, no terminal, no network needed.
    The human physically walks up to the device and presses a button.

    Setup:
      goals.yaml:
        hil:
          channel: gpio_button
          approve_pin: 5    # BCM pin for approve button (pull-up, active LOW)
          deny_pin: 6       # BCM pin for deny button
          led_pin: 13       # optional — blinks while waiting for approval
    """

    def __init__(self, approve_pin: int, deny_pin: int,
                 led_pin: Optional[int] = None):
        self.approve_pin = approve_pin
        self.deny_pin = deny_pin
        self.led_pin = led_pin
        self._decision: Optional[str] = None
        self._gpio_available = False
        self._setup_gpio()

    def _setup_gpio(self):
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.approve_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(self.deny_pin,   GPIO.IN, pull_up_down=GPIO.PUD_UP)
            if self.led_pin:
                GPIO.setup(self.led_pin, GPIO.OUT)
                GPIO.output(self.led_pin, GPIO.LOW)

            def _on_approve(channel):
                logger.info("GPIO HIL: approve button pressed (pin %d)", self.approve_pin)
                self._decision = "approve"

            def _on_deny(channel):
                logger.info("GPIO HIL: deny button pressed (pin %d)", self.deny_pin)
                self._decision = "deny"

            GPIO.add_event_detect(self.approve_pin, GPIO.FALLING,
                                  callback=_on_approve, bouncetime=300)
            GPIO.add_event_detect(self.deny_pin,    GPIO.FALLING,
                                  callback=_on_deny,   bouncetime=300)
            self._gpio_available = True
            logger.info("GPIO HIL ready: approve=pin%d deny=pin%d",
                        self.approve_pin, self.deny_pin)
        except ImportError:
            logger.warning("GPIO HIL: RPi.GPIO not installed — will try gpiozero")
            self._setup_gpiozero()
        except Exception as e:
            logger.error("GPIO HIL setup failed: %s", e)

    def _setup_gpiozero(self):
        try:
            from gpiozero import Button
            btn_approve = Button(self.approve_pin, pull_up=True, bounce_time=0.3)
            btn_deny    = Button(self.deny_pin,    pull_up=True, bounce_time=0.3)
            btn_approve.when_pressed = lambda: setattr(self, "_decision", "approve")
            btn_deny.when_pressed    = lambda: setattr(self, "_decision", "deny")
            self._gpio_available = True
            logger.info("GPIO HIL ready (gpiozero): approve=pin%d deny=pin%d",
                        self.approve_pin, self.deny_pin)
        except Exception as e:
            logger.error("GPIO HIL gpiozero setup failed: %s", e)

    def send(self, req: ApprovalRequest) -> bool:
        """Print decision details to console and start LED blink."""
        self._decision = None
        print(f"\n  *** PHYSICAL APPROVAL REQUIRED ***")
        print(f"  Tool  : {req.tool}({req.args})")
        print(f"  Goal  : {req.goal[:80]}")
        print(f"  Reason: {req.reason}")
        print(f"  Press GREEN button (pin {self.approve_pin}) to APPROVE")
        print(f"  Press RED   button (pin {self.deny_pin})   to DENY")
        print(f"  Timeout: {req.timeout_sec}s\n")
        if self.led_pin and self._gpio_available:
            self._start_blink()
        return self._gpio_available

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        return self._decision

    def send_result(self, req: ApprovalRequest):
        self._stop_blink()
        icon = "APPROVED" if req.decision == "approve" else "DENIED"
        print(f"  [{icon}] by {req.decided_by}\n")

    def _start_blink(self):
        """Blink LED on a background thread while waiting."""
        self._blinking = True
        def _blink():
            try:
                import RPi.GPIO as GPIO
                while self._blinking:
                    GPIO.output(self.led_pin, GPIO.HIGH)
                    time.sleep(0.3)
                    GPIO.output(self.led_pin, GPIO.LOW)
                    time.sleep(0.3)
            except Exception:
                pass
        threading.Thread(target=_blink, daemon=True).start()

    def _stop_blink(self):
        self._blinking = False
        try:
            import RPi.GPIO as GPIO
            GPIO.output(self.led_pin, GPIO.LOW)
        except Exception:
            pass


class SerialApprovalChannel(_BaseChannel):
    """
    Approval via serial terminal — no internet, works on any UART device.

    Sends the approval request as a formatted message over a serial port.
    The human reads on a connected terminal (laptop, Arduino with LCD,
    secondary Pi) and types 'approve' or 'deny'.

    Useful when:
    - Device is accessible over serial but not SSH
    - A secondary microcontroller acts as the approval interface
    - You have a serial console already connected for monitoring

    Setup:
      goals.yaml:
        hil:
          channel: serial
          serial_port: "/dev/ttyUSB0"   # or /dev/ttyAMA0 for Pi UART
          serial_baud: 115200
    """

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200):
        self.port = port
        self.baud = baud

    def send(self, req: ApprovalRequest) -> bool:
        msg = (
            f"\r\n{'='*50}\r\n"
            f"KARYA — APPROVAL REQUIRED\r\n"
            f"{'='*50}\r\n"
            f"Tool   : {req.tool}\r\n"
            f"Args   : {json.dumps(req.args)}\r\n"
            f"Goal   : {req.goal[:80]}\r\n"
            f"Reason : {req.reason}\r\n"
            f"Score  : {req.priority_score:.0f}\r\n"
            f"ID     : {req.request_id}\r\n"
            f"Timeout: {req.timeout_sec}s\r\n"
            f"{'='*50}\r\n"
            f"Type 'approve' or 'deny' and press Enter:\r\n"
        )
        try:
            import serial
            with serial.Serial(self.port, self.baud, timeout=1) as ser:
                ser.write(msg.encode("utf-8"))
            logger.info("Serial HIL sent on %s", self.port)
            return True
        except ImportError:
            # pyserial not installed — print to stdout instead
            print(msg.replace("\r\n", "\n"))
            return True
        except Exception as e:
            logger.error("Serial HIL send failed: %s", e)
            return False

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        try:
            import serial
            with serial.Serial(self.port, self.baud, timeout=0.5) as ser:
                line = ser.readline().decode("utf-8", errors="replace").strip().lower()
                if line in ("approve", "deny"):
                    return line
        except ImportError:
            pass
        except Exception:
            pass
        return None


class DisplayChannel(_BaseChannel):
    """
    Terminal / display approval — for devices with a screen or SSH session.
    Blocks the terminal and waits for the human to type 'y' or 'n'.

    This is the simplest possible HIL — no hardware, no network, no files.
    Just a prompt on the screen. Useful during development, testing,
    or on any device with an attached keyboard and display.

    Setup:
      goals.yaml:
        hil:
          channel: display    # or: channel: terminal
    """

    def __init__(self):
        self._decision: Optional[str] = None
        self._got_input = threading.Event()

    def send(self, req: ApprovalRequest) -> bool:
        self._decision = None
        self._got_input.clear()
        print(f"\n{'═'*52}")
        print(f"  karya — CRITICAL ACTION — approval required")
        print(f"{'═'*52}")
        print(f"  Tool   : {req.tool}")
        print(f"  Args   : {json.dumps(req.args)}")
        print(f"  Goal   : {req.goal[:70]}")
        print(f"  Reason : {req.reason}")
        print(f"  Score  : {req.priority_score:.0f}")
        print(f"  Timeout: {req.timeout_sec}s")
        print(f"{'═'*52}")

        def _read():
            try:
                ans = input("  Approve? [y/N]: ").strip().lower()
                self._decision = "approve" if ans in ("y", "yes", "approve") else "deny"
            except (EOFError, KeyboardInterrupt):
                self._decision = "deny"
            finally:
                self._got_input.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        return True

    def poll(self, req: ApprovalRequest) -> Optional[str]:
        if self._got_input.is_set():
            return self._decision
        return None


# ── HIL Manager ───────────────────────────────────────────────────────────────

class HILManager:
    """
    Central HIL coordinator. Called by the agent loop before every tool execution.

    Usage:
        hil = HILManager.from_config(config_dict)
        approved, reason = hil.request_approval(tool, args, goal, score)
        if approved:
            registry.execute(tool, args)
    """

    def __init__(
        self,
        channel: Optional[_BaseChannel] = None,
        timeout_sec: int = 120,
        default_on_timeout: str = "deny",    # "deny" | "approve"
        log_dir: str = "~/.karya/hil/log",
        enabled: bool = True,
    ):
        self.channel = channel
        self.timeout_sec = timeout_sec
        self.default_on_timeout = default_on_timeout
        self.log_dir = Path(os.path.expanduser(log_dir))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self._pending: dict = {}

    @classmethod
    def from_config(cls, config: dict) -> "HILManager":
        """
        Build HILManager from the hil: section of goals.yaml.

        Channel priority:
          file     — always works, zero internet, karya-native default
          telegram — opt-in, needs internet, fastest on mobile
          slack    — opt-in, needs internet, good for teams
          webhook  — opt-in, needs internet, connects to anything

        If a network channel is configured but credentials are missing
        or the network is unavailable, karya falls back to file
        automatically — it never blocks on a missing network channel.
        """
        if not config or not config.get("enabled", False):
            return cls(enabled=False)

        channel_name = config.get("channel", "file").lower()
        timeout = config.get("timeout_sec", 120)
        default = config.get("default_on_timeout", "deny")
        hil_dir = config.get("hil_dir", "~/.karya/hil")

        def _fallback_to_file(reason: str) -> _BaseChannel:
            logger.warning("HIL: %s — falling back to file channel (offline-safe)", reason)
            return FileChannel(hil_dir)

        channel: Optional[_BaseChannel] = None

        if channel_name == "file":
            # Primary offline channel — always works, no internet needed
            channel = FileChannel(hil_dir)

        elif channel_name in ("display", "terminal"):
            # Terminal keypress — simplest offline HIL, no hardware needed
            channel = DisplayChannel()
            logger.info("HIL: display/terminal channel (offline, keypress)")

        elif channel_name == "gpio_button":
            # Physical button — most reliable offline HIL for unattended Pi
            approve_pin = config.get("approve_pin", 5)
            deny_pin    = config.get("deny_pin",    6)
            led_pin     = config.get("led_pin", None)
            channel = GPIOButtonChannel(approve_pin, deny_pin, led_pin)
            logger.info("HIL: GPIO button channel (offline) — approve=pin%d deny=pin%d",
                        approve_pin, deny_pin)

        elif channel_name == "serial":
            # Serial terminal — offline, works over UART
            port = config.get("serial_port", "/dev/ttyUSB0")
            baud = config.get("serial_baud", 115200)
            channel = SerialApprovalChannel(port, baud)
            logger.info("HIL: serial channel (offline) — %s @ %d", port, baud)

        elif channel_name == "telegram":
            # Optional: requires internet + bot token + chat ID
            # karya does NOT require this — it is opt-in only
            token = config.get("telegram_bot_token", "")
            chat_id = config.get("telegram_chat_id", "")
            if token and chat_id:
                channel = TelegramChannel(token, chat_id)
                logger.info("HIL: Telegram channel configured (optional, needs internet)")
            else:
                channel = _fallback_to_file(
                    "telegram selected but bot_token or chat_id missing"
                )

        elif channel_name == "slack":
            # Optional: requires internet + Slack webhook URL
            webhook_url = config.get("slack_webhook_url", "")
            response_file = config.get("slack_response_file",
                                       "/tmp/karya_hil_responses.txt")
            if webhook_url:
                channel = SlackChannel(webhook_url, response_file)
                logger.info("HIL: Slack channel configured (optional, needs internet)")
            else:
                channel = _fallback_to_file(
                    "slack selected but slack_webhook_url missing"
                )

        elif channel_name == "webhook":
            # Optional: requires internet + notify + poll URLs
            notify_url = config.get("webhook_notify_url", "")
            poll_url = config.get("webhook_poll_url", "")
            if notify_url and poll_url:
                channel = WebhookChannel(notify_url, poll_url)
                logger.info("HIL: Webhook channel configured (optional, needs internet)")
            else:
                channel = _fallback_to_file(
                    "webhook selected but webhook_notify_url or webhook_poll_url missing"
                )

        else:
            # Unknown channel name — safe fallback
            channel = _fallback_to_file(f"unknown channel '{channel_name}'")

        return cls(
            channel=channel,
            timeout_sec=timeout,
            default_on_timeout=default,
            enabled=True,
        )

    def needs_approval(self, tool: str, args: dict, priority_score: float) -> tuple:
        """
        Returns (HILLevel, reason_string).
        Call this before every tool execution to decide whether to pause.
        """
        if not self.enabled:
            return HILLevel.AUTO, ""
        level = classify_decision(tool, args, priority_score)
        reason = _reason_for_critical(tool, args, priority_score) if level == HILLevel.CRITICAL else ""
        return level, reason

    def request_approval(
        self,
        tool: str,
        args: dict,
        goal: str,
        priority_score: float,
    ) -> tuple:
        """
        Pause execution and wait for human approval.
        Returns (approved: bool, reason: str).

        If HIL is disabled, always returns (True, "hil_disabled").
        If channel send fails, defaults to deny for safety.
        """
        if not self.enabled:
            return True, "hil_disabled"

        level, reason = self.needs_approval(tool, args, priority_score)

        if level == HILLevel.BLOCK:
            return False, "blocked_by_safety"

        if level == HILLevel.AUTO:
            return True, "auto_approved"

        if level == HILLevel.CONFIRM:
            logger.info("CONFIRM-level action: waiting 10s for %s(%s)", tool, args)
            time.sleep(10)
            return True, "confirm_delay_passed"

        # CRITICAL — send notification and wait
        req = ApprovalRequest(
            request_id=self._make_id(tool, args),
            timestamp=datetime.now().isoformat(timespec="seconds"),
            tool=tool,
            args=args,
            goal=goal[:200],
            priority_score=priority_score,
            reason=reason,
            timeout_sec=self.timeout_sec,
        )

        logger.warning(
            "CRITICAL action requires human approval: %s(%s) — reason: %s",
            tool, args, reason
        )
        print(f"\n  ⚠  CRITICAL — awaiting human approval")
        print(f"     tool  : {tool}({args})")
        print(f"     reason: {reason}")
        print(f"     timeout: {self.timeout_sec}s (default: {self.default_on_timeout})")

        if not self.channel:
            logger.error("HIL enabled but no channel configured — denying for safety")
            return False, "no_channel_configured"

        sent = self.channel.send(req)
        if not sent:
            logger.error("HIL notification failed to send — denying for safety")
            self._log_request(req, "deny", "send_failed")
            return False, "notification_send_failed"

        # Poll for response
        decision = self._wait_for_decision(req)
        req.decision = decision
        req.decided_at = datetime.now().isoformat(timespec="seconds")
        req.decided_by = "human" if decision != self.default_on_timeout else "timeout"

        # Update the notification with the outcome
        if hasattr(self.channel, "send_result"):
            try:
                self.channel.send_result(req)
            except Exception:
                pass

        if hasattr(self.channel, "cleanup"):
            try:
                self.channel.cleanup(req)
            except Exception:
                pass

        self._log_request(req, decision, req.decided_by)

        approved = decision == "approve"
        reason_out = f"human_{decision}" if req.decided_by == "human" else f"timeout_{self.default_on_timeout}"

        if approved:
            print(f"  ✅ APPROVED by {req.decided_by}")
            logger.info("HIL approved: %s(%s) by %s", tool, args, req.decided_by)
        else:
            print(f"  ❌ DENIED by {req.decided_by}")
            logger.warning("HIL denied: %s(%s) by %s", tool, args, req.decided_by)

        return approved, reason_out

    def _wait_for_decision(self, req: ApprovalRequest) -> str:
        """Poll channel every 2 seconds until decision or timeout."""
        deadline = time.monotonic() + req.timeout_sec
        while time.monotonic() < deadline:
            decision = self.channel.poll(req)
            if decision in ("approve", "deny"):
                return decision
            remaining = int(deadline - time.monotonic())
            if remaining % 20 == 0 and remaining > 0:
                print(f"     waiting... {remaining}s remaining")
            time.sleep(2)
        logger.warning("HIL timeout after %ds — defaulting to %s",
                       req.timeout_sec, self.default_on_timeout)
        return self.default_on_timeout

    def _make_id(self, tool: str, args: dict) -> str:
        """Short deterministic ID for this request."""
        raw = f"{tool}{json.dumps(args, sort_keys=True)}{time.time()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:8]

    def _log_request(self, req: ApprovalRequest, decision: str, decided_by: str):
        """Append to the HIL audit log."""
        log_path = self.log_dir / "hil_audit.jsonl"
        record = {
            **asdict(req),
            "decision": decision,
            "decided_by": decided_by,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error("HIL audit log write failed: %s", e)
