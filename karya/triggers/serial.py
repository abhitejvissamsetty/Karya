"""
triggers/serial.py — serial port trigger
Listens on a UART or USB serial port for incoming messages.
Fires the agent when a message arrives.

Common sources:
- Arduino sending sensor readings
- RS-485 industrial sensors
- GPS modules
- Custom hardware
- Another Pi sending commands via GPIO UART

Gracefully degrades if pyserial not installed or port unavailable.

Usage:
    trigger = SerialTrigger(
        port="/dev/ttyUSB0",
        baud=9600,
        trigger_on="any",        # fire on any message
    )
    # Or fire only when message contains a keyword:
    trigger = SerialTrigger(
        port="/dev/ttyAMA0",
        baud=115200,
        trigger_on="keyword",
        keywords=["ALERT", "TEMP:", "ERROR"],
    )
"""

import logging
import time
from typing import Optional

from karya.triggers.base import BaseTrigger, TriggerCallback

logger = logging.getLogger("karya.triggers.serial")


class SerialTrigger(BaseTrigger):
    """
    Reads lines from a serial port and fires on matching messages.

    Args:
        port:           serial device path, e.g. /dev/ttyUSB0, /dev/ttyAMA0
        baud:           baud rate (default 9600)
        trigger_on:     "any" fires on every line | "keyword" fires on keyword match
        keywords:       list of strings to match (case-insensitive) if trigger_on="keyword"
        timeout:        read timeout in seconds
        encoding:       serial line encoding
        max_message_len: truncate long messages to this length
    """

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baud: int = 9600,
        trigger_on: str = "any",
        keywords: Optional[list] = None,
        timeout: float = 1.0,
        encoding: str = "utf-8",
        max_message_len: int = 256,
        callback: Optional[TriggerCallback] = None,
    ):
        super().__init__(name=f"serial:{port}", callback=callback)
        self.port = port
        self.baud = baud
        self.trigger_on = trigger_on
        self.keywords = [k.lower() for k in (keywords or [])]
        self.timeout = timeout
        self.encoding = encoding
        self.max_message_len = max_message_len

    def _run(self):
        if self._try_pyserial():
            return
        logger.warning(
            "pyserial not installed or port %s unavailable. "
            "Install with: pip install pyserial",
            self.port,
        )
        self._stop_event.wait()

    def _try_pyserial(self) -> bool:
        try:
            import serial
        except ImportError:
            logger.debug("pyserial not installed")
            return False

        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("Cannot open serial port %s: %s", self.port, e)
            return False

        logger.info("Serial trigger listening on %s @ %d baud", self.port, self.baud)

        try:
            while not self._stop_event.is_set():
                try:
                    raw = ser.readline()
                    if not raw:
                        continue
                    message = raw.decode(self.encoding, errors="replace").strip()
                    if not message:
                        continue

                    # truncate long messages
                    if len(message) > self.max_message_len:
                        message = message[:self.max_message_len] + "…"

                    should_fire = (
                        self.trigger_on == "any"
                        or (
                            self.trigger_on == "keyword"
                            and any(kw in message.lower() for kw in self.keywords)
                        )
                    )

                    if should_fire:
                        matched_kw = [
                            kw for kw in self.keywords if kw in message.lower()
                        ]
                        self.fire(
                            reason=f"serial message on {self.port}: {message[:60]}",
                            data={
                                "port": self.port,
                                "message": message,
                                "matched_keywords": matched_kw,
                            },
                        )

                except Exception as e:
                    logger.debug("Serial read error: %s", e)
                    time.sleep(0.5)

        finally:
            try:
                ser.close()
            except Exception:
                pass

        return True

    def send(self, message: str):
        """Send a message back over the serial port (if pyserial available)."""
        try:
            import serial
            ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=1)
            ser.write((message + "\n").encode(self.encoding))
            ser.close()
            logger.debug("Sent serial message: %r", message)
        except Exception as e:
            logger.error("Serial send failed: %s", e)
