"""
tools/gpio.py — GPIO read/write tool for agent actions
Lets the agent control hardware: relays, LEDs, buzzers, motors.
Reads from sensors connected to input pins.
Gracefully stubs on non-Pi hardware.
"""

import logging
import time

logger = logging.getLogger("karya.tools.gpio")


class GPIOTool:
    name = "gpio"
    description = "Read or write a Raspberry Pi GPIO pin. Use for hardware control."
    schema = {
        "type": "function",
        "function": {
            "name": "gpio",
            "description": "Read from or write to a GPIO pin",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "pulse"],
                        "description": "read: get pin state | write: set pin HIGH/LOW | pulse: brief pulse",
                    },
                    "pin": {
                        "type": "integer",
                        "description": "BCM pin number",
                    },
                    "value": {
                        "type": "integer",
                        "enum": [0, 1],
                        "description": "1=HIGH, 0=LOW (write/pulse only)",
                    },
                    "duration_ms": {
                        "type": "integer",
                        "description": "Pulse duration in milliseconds (pulse only, default 200)",
                    },
                },
                "required": ["action", "pin"],
            },
        },
    }

    def run(self, action: str, pin: int, value: int = 1, duration_ms: int = 200) -> str:
        if action == "read":
            return self._read(pin)
        if action == "write":
            return self._write(pin, value)
        if action == "pulse":
            return self._pulse(pin, value, duration_ms)
        return f"[unknown action: {action}]"

    def _read(self, pin: int) -> str:
        # Try RPi.GPIO
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.IN)
            state = GPIO.input(pin)
            return f"pin {pin} = {'HIGH' if state else 'LOW'} ({state})"
        except ImportError:
            pass

        # Try gpiozero
        try:
            from gpiozero import DigitalInputDevice
            d = DigitalInputDevice(pin)
            state = int(d.value)
            d.close()
            return f"pin {pin} = {'HIGH' if state else 'LOW'} ({state})"
        except (ImportError, Exception):
            pass

        # Try sysfs
        try:
            with open(f"/sys/class/gpio/gpio{pin}/value") as f:
                state = int(f.read().strip())
            return f"pin {pin} = {'HIGH' if state else 'LOW'} ({state}) [sysfs]"
        except Exception:
            pass

        return f"[gpio unavailable — not a Raspberry Pi or no library installed]"

    def _write(self, pin: int, value: int) -> str:
        # Try RPi.GPIO
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH if value else GPIO.LOW)
            label = "HIGH" if value else "LOW"
            logger.info("GPIO pin %d set %s", pin, label)
            return f"pin {pin} set to {label}"
        except ImportError:
            pass

        # Try gpiozero
        try:
            from gpiozero import DigitalOutputDevice
            d = DigitalOutputDevice(pin)
            if value:
                d.on()
            else:
                d.off()
            d.close()
            return f"pin {pin} set to {'HIGH' if value else 'LOW'} [gpiozero]"
        except (ImportError, Exception):
            pass

        # Try sysfs
        try:
            export = "/sys/class/gpio/export"
            direction = f"/sys/class/gpio/gpio{pin}/direction"
            val_path = f"/sys/class/gpio/gpio{pin}/value"
            import os
            if not os.path.exists(val_path):
                with open(export, "w") as f:
                    f.write(str(pin))
                time.sleep(0.1)
            with open(direction, "w") as f:
                f.write("out")
            with open(val_path, "w") as f:
                f.write(str(value))
            return f"pin {pin} set to {value} [sysfs]"
        except Exception as e:
            return f"[sysfs gpio write failed: {e}]"

    def _pulse(self, pin: int, value: int, duration_ms: int) -> str:
        on_result = self._write(pin, value)
        if "[" in on_result and "error" in on_result.lower():
            return on_result
        time.sleep(duration_ms / 1000)
        off_result = self._write(pin, 1 - value)
        return f"pulsed pin {pin} for {duration_ms}ms"
