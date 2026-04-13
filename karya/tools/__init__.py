"""
tools/ — offline tool implementations
All tools work with zero network. Each returns a compact string result.
"""

import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


# ── Shell Tool ────────────────────────────────────────────────────────────────

class ShellTool:
    name = "shell"
    description = "Run a shell command and return stdout+stderr. Use for system tasks."
    schema = {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"}
                },
                "required": ["command"],
            },
        },
    }

    def __init__(self, timeout: int = 30, max_output_chars: int = 2000):
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    def run(self, command: str) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = result.stdout + result.stderr
            if len(output) > self.max_output_chars:
                output = output[:self.max_output_chars] + "\n…[truncated]"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"[timeout after {self.timeout}s]"
        except Exception as e:
            return f"[error: {e}]"


# ── File Tool ─────────────────────────────────────────────────────────────────

class FileTool:
    name = "file"
    description = "Read or write files on disk."
    schema = {
        "type": "function",
        "function": {
            "name": "file",
            "description": "Read or write a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write", "append", "exists"]},
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "Content to write (write/append only)"},
                },
                "required": ["action", "path"],
            },
        },
    }

    def __init__(self, max_read_chars: int = 4096,
                 allowed_write_dirs: Optional[list[str]] = None):
        self.max_read_chars = max_read_chars
        self.allowed_write_dirs = allowed_write_dirs or [
            str(Path.home()),
            "/tmp",
            "/var/log/karya",
        ]

    def run(self, action: str, path: str, content: str = "") -> str:
        path = os.path.expanduser(path)
        if action == "exists":
            return str(os.path.exists(path))
        if action == "read":
            try:
                with open(path) as f:
                    text = f.read(self.max_read_chars)
                if len(text) == self.max_read_chars:
                    text += "\n…[file truncated]"
                return text
            except Exception as e:
                return f"[read error: {e}]"
        if action in ("write", "append"):
            if not any(path.startswith(d) for d in self.allowed_write_dirs):
                return f"[blocked: {path} not in allowed write dirs]"
            mode = "w" if action == "write" else "a"
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, mode) as f:
                    f.write(content)
                return f"ok — {len(content)} chars {action}ten to {path}"
            except Exception as e:
                return f"[write error: {e}]"
        return f"[unknown action: {action}]"


# ── System Info Tool ──────────────────────────────────────────────────────────

class SystemInfoTool:
    name = "system_info"
    description = "Get CPU, memory, disk, temperature info from this machine."
    schema = {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get current system metrics: cpu, memory, disk, temp",
            "parameters": {
                "type": "object",
                "properties": {
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["cpu", "memory", "disk", "temp", "processes", "all"]},
                        "description": "Which metrics to fetch"
                    }
                },
                "required": ["metrics"],
            },
        },
    }

    def run(self, metrics: list[str]) -> str:
        if "all" in metrics:
            metrics = ["cpu", "memory", "disk", "temp"]
        results = {}
        for m in metrics:
            results[m] = self._get(m)
        return json.dumps(results, indent=2)

    def _get(self, metric: str) -> dict:
        if metric == "cpu":
            return self._cpu()
        if metric == "memory":
            return self._memory()
        if metric == "disk":
            return self._disk()
        if metric == "temp":
            return self._temp()
        if metric == "processes":
            return self._processes()
        return {}

    def _cpu(self) -> dict:
        try:
            # Read /proc/stat for CPU usage
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            time.sleep(0.1)
            with open("/proc/stat") as f:
                line = f.readline()
            vals2 = list(map(int, line.split()[1:]))
            idle2 = vals2[3]
            total2 = sum(vals2)
            usage = 100 - (idle2 - idle) / (total2 - total) * 100
            return {"usage_pct": round(usage, 1)}
        except Exception:
            return {"usage_pct": None}

    def _memory(self) -> dict:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith(("MemTotal", "MemFree", "MemAvailable")):
                        k, v = line.split(":")
                        info[k.strip()] = int(v.split()[0]) * 1024  # bytes
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            used = total - avail
            pct = round(used / total * 100, 1) if total else 0
            return {
                "total_mb": round(total / 1e6),
                "used_mb": round(used / 1e6),
                "free_mb": round(avail / 1e6),
                "used_pct": pct,
            }
        except Exception:
            return {}

    def _disk(self) -> dict:
        try:
            total, used, free = shutil.disk_usage("/")
            pct = round(used / total * 100, 1)
            return {
                "total_gb": round(total / 1e9, 1),
                "used_gb": round(used / 1e9, 1),
                "free_gb": round(free / 1e9, 1),
                "used_pct": pct,
            }
        except Exception:
            return {}

    def _temp(self) -> dict:
        # Raspberry Pi thermal zone
        paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]
        for p in paths:
            try:
                with open(p) as f:
                    raw = int(f.read().strip())
                temp = raw / 1000.0  # millidegrees → Celsius
                return {"cpu_temp_c": round(temp, 1)}
            except Exception:
                continue
        return {"cpu_temp_c": None}

    def _processes(self) -> dict:
        try:
            result = subprocess.run(
                ["ps", "aux", "--sort=-%mem"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")[:6]  # top 5 + header
            return {"top_processes": lines}
        except Exception:
            return {}


# ── Tool Registry ─────────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self, safety_guard=None, tool_result_max_chars: int = 2000):
        self.tools = {}
        self.safety = safety_guard
        self.max_chars = tool_result_max_chars

    def register(self, tool):
        self.tools[tool.name] = tool

    def get_schemas(self) -> list[dict]:
        return [t.schema for t in self.tools.values()]

    def execute(self, tool_name: str, args: dict) -> tuple[str, bool]:
        """
        Execute a tool. Returns (result_string, success_bool).
        All safety checks happen here before dispatch.
        """
        tool = self.tools.get(tool_name)
        if not tool:
            return f"[unknown tool: {tool_name}]", False

        # safety gate for shell
        if tool_name == "shell" and self.safety:
            command = args.get("command", "")
            ok, msg = self.safety.approve_shell(command)
            if not ok:
                return f"[blocked by safety: {msg}]", False

        # safety gate for file writes
        if tool_name == "file" and args.get("action") in ("write", "append") and self.safety:
            ok, msg = self.safety.approve_file_write(args.get("path", ""))
            if not ok:
                return f"[blocked by safety: {msg}]", False

        try:
            result = tool.run(**args)
            if len(result) > self.max_chars:
                result = result[:self.max_chars] + "\n…[truncated]"
            return result, True
        except Exception as e:
            return f"[tool error: {e}]", False
