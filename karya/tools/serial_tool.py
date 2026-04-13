"""
tools/serial_tool.py — serial port communication tool
Lets the agent send commands to and read from serial devices.
Useful for: talking to Arduino, controlling RS-485 devices, reading GPS.
"""

import logging
import time

logger = logging.getLogger("karya.tools.serial")


class SerialTool:
    name = "serial"
    description = "Send or read from a serial port. For Arduino, sensors, and hardware."
    schema = {
        "type": "function",
        "function": {
            "name": "serial",
            "description": "Send a command to a serial device or read its output",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["send", "read", "send_and_read"],
                    },
                    "port": {
                        "type": "string",
                        "description": "Serial port path, e.g. /dev/ttyUSB0",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message to send (send/send_and_read only)",
                    },
                    "baud": {
                        "type": "integer",
                        "description": "Baud rate (default 9600)",
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": "How long to wait for response (default 2s)",
                    },
                },
                "required": ["action", "port"],
            },
        },
    }

    def run(
        self,
        action: str,
        port: str,
        message: str = "",
        baud: int = 9600,
        timeout_sec: float = 2.0,
    ) -> str:
        try:
            import serial
        except ImportError:
            return "[pyserial not installed. Run: pip install pyserial]"

        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=timeout_sec)
        except Exception as e:
            return f"[cannot open {port}: {e}]"

        try:
            if action == "send":
                ser.write((message + "\n").encode("utf-8"))
                return f"sent to {port}: {message!r}"

            if action == "read":
                lines = []
                deadline = time.monotonic() + timeout_sec
                while time.monotonic() < deadline:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)
                return "\n".join(lines) if lines else "[no data received]"

            if action == "send_and_read":
                ser.write((message + "\n").encode("utf-8"))
                time.sleep(0.1)
                lines = []
                deadline = time.monotonic() + timeout_sec
                while time.monotonic() < deadline:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)
                return "\n".join(lines) if lines else "[no response]"

            return f"[unknown action: {action}]"

        except Exception as e:
            return f"[serial error: {e}]"
        finally:
            try:
                ser.close()
            except Exception:
                pass
