"""
triggers/gpio.py — GPIO pin event trigger (Raspberry Pi)
Fires when a GPIO pin changes state (button press, sensor signal, etc.)

Gracefully degrades on non-Pi hardware:
- If RPi.GPIO is not installed → simulation mode (logs, never fires)
- If gpiozero is available → uses that instead (cleaner API)
- Falls back to polling /sys/class/gpio if neither library is present

Usage:
    # Fire when pin 17 goes LOW (e.g. button press with pull-up)
    trigger = GPIOTrigger(pin=17, edge="falling", pull_up=True)
    trigger.set_callback(on_event)
    trigger.start()

    # Fire when pin 24 goes HIGH (sensor output)
    trigger = GPIOTrigger(pin=24, edge="rising")
"""

import logging
import time
from typing import Optional

from karya.triggers.base import BaseTrigger, TriggerCallback

logger = logging.getLogger("karya.triggers.gpio")

EDGE_CHOICES = ("rising", "falling", "both")


class GPIOTrigger(BaseTrigger):
    """
    Monitors a GPIO input pin and fires on edge transitions.

    Args:
        pin:        BCM pin number
        edge:       "rising" | "falling" | "both"
        pull_up:    enable internal pull-up resistor
        debounce_ms: ignore re-triggers within this window
        callback:   function to call on event
    """

    def __init__(
        self,
        pin: int,
        edge: str = "falling",
        pull_up: bool = True,
        debounce_ms: int = 200,
        callback: Optional[TriggerCallback] = None,
    ):
        super().__init__(name=f"gpio:pin{pin}", callback=callback)
        if edge not in EDGE_CHOICES:
            raise ValueError(f"edge must be one of {EDGE_CHOICES}")
        self.pin = pin
        self.edge = edge
        self.pull_up = pull_up
        self.debounce_ms = debounce_ms
        self._last_fire_time = 0.0

    def _run(self):
        # Try RPi.GPIO first
        if self._try_rpigpio():
            return
        # Try gpiozero
        if self._try_gpiozero():
            return
        # Try sysfs polling
        if self._try_sysfs():
            return
        # Simulation mode
        logger.warning(
            "GPIO not available on this hardware. "
            "Pin %d trigger running in simulation mode (never fires). "
            "Install RPi.GPIO or gpiozero on a Raspberry Pi.",
            self.pin,
        )
        # Just block until stopped
        self._stop_event.wait()

    # ── RPi.GPIO backend ──────────────────────────────────────────────────────

    def _try_rpigpio(self) -> bool:
        try:
            import RPi.GPIO as GPIO

            GPIO.setmode(GPIO.BCM)
            pud = GPIO.PUD_UP if self.pull_up else GPIO.PUD_DOWN
            GPIO.setup(self.pin, GPIO.IN, pull_up_down=pud)

            edge_map = {
                "rising":  GPIO.RISING,
                "falling": GPIO.FALLING,
                "both":    GPIO.BOTH,
            }

            def _callback(channel):
                now = time.monotonic()
                if (now - self._last_fire_time) * 1000 < self.debounce_ms:
                    return
                self._last_fire_time = now
                state = GPIO.input(self.pin)
                self.fire(
                    reason=f"pin {self.pin} {'HIGH' if state else 'LOW'} ({self.edge} edge)",
                    data={"pin": self.pin, "state": state, "edge": self.edge},
                )

            GPIO.add_event_detect(
                self.pin,
                edge_map[self.edge],
                callback=_callback,
                bouncetime=self.debounce_ms,
            )
            logger.info("RPi.GPIO watching pin %d (%s edge)", self.pin, self.edge)

            # Block until stopped
            self._stop_event.wait()
            GPIO.remove_event_detect(self.pin)
            GPIO.cleanup(self.pin)
            return True

        except (ImportError, RuntimeError):
            return False

    # ── gpiozero backend ──────────────────────────────────────────────────────

    def _try_gpiozero(self) -> bool:
        try:
            from gpiozero import Button, DigitalInputDevice

            if self.pull_up:
                device = Button(self.pin, pull_up=True, bounce_time=self.debounce_ms / 1000)
            else:
                device = DigitalInputDevice(self.pin, pull_up=False)

            def _on_press():
                self.fire(
                    reason=f"pin {self.pin} activated (gpiozero)",
                    data={"pin": self.pin, "edge": self.edge},
                )

            if self.edge in ("falling", "both"):
                device.when_pressed = _on_press
            if self.edge in ("rising", "both"):
                device.when_released = _on_press

            logger.info("gpiozero watching pin %d (%s edge)", self.pin, self.edge)
            self._stop_event.wait()
            device.close()
            return True

        except (ImportError, Exception):
            return False

    # ── sysfs polling fallback ────────────────────────────────────────────────

    def _try_sysfs(self) -> bool:
        """Poll /sys/class/gpio — works without any library, slower."""
        gpio_path = f"/sys/class/gpio/gpio{self.pin}/value"
        export_path = "/sys/class/gpio/export"

        try:
            # Export pin if needed
            if not __import__("os").path.exists(gpio_path):
                with open(export_path, "w") as f:
                    f.write(str(self.pin))
                time.sleep(0.1)
                direction_path = f"/sys/class/gpio/gpio{self.pin}/direction"
                with open(direction_path, "w") as f:
                    f.write("in")

            if not __import__("os").path.exists(gpio_path):
                return False

            logger.info("sysfs polling pin %d (%s edge)", self.pin, self.edge)
            last_state = self._read_sysfs(gpio_path)

            while not self._stop_event.wait(timeout=0.05):
                state = self._read_sysfs(gpio_path)
                if state is None:
                    continue

                fired = False
                if self.edge == "rising" and state == 1 and last_state == 0:
                    fired = True
                elif self.edge == "falling" and state == 0 and last_state == 1:
                    fired = True
                elif self.edge == "both" and state != last_state:
                    fired = True

                if fired:
                    now = time.monotonic()
                    if (now - self._last_fire_time) * 1000 >= self.debounce_ms:
                        self._last_fire_time = now
                        self.fire(
                            reason=f"pin {self.pin} {'HIGH' if state else 'LOW'} (sysfs)",
                            data={"pin": self.pin, "state": state},
                        )
                last_state = state

            return True

        except Exception:
            return False

    def _read_sysfs(self, path: str) -> Optional[int]:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except Exception:
            return None
