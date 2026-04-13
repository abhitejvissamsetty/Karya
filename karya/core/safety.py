"""
safety.py — guard rails for autonomous action execution
Every tool call passes through here before execution.
No human in the loop, so we need hard limits baked in.
"""

import re
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger("karya.safety")


class SafetyLevel(Enum):
    SAFE = "safe"           # execute immediately
    CONFIRM = "confirm"     # log, wait, then execute
    FORBIDDEN = "forbidden" # never execute, log and skip


# Patterns matched against the full command string
FORBIDDEN_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"dd\s+if=",
    r"mkfs\.",
    r":\(\)\{.*\}",        # fork bomb
    r"chmod\s+-R\s+777\s+/",
    r">\s*/dev/sd",
    r"fdisk",
    r"parted",
    r"shutdown\s+-h\s+now",
    r"reboot",
    r"halt",
    r"curl\s+.*\|\s*sh",   # remote code execution
    r"wget\s+.*-O-.*\|",
]

# Shell commands that need a 10s pause before executing
CONFIRM_PATTERNS = [
    r"^rm\b",
    r"^kill\b",
    r"^pkill\b",
    r"systemctl\s+stop",
    r"systemctl\s+disable",
    r"truncate\b",
    r"^mv\b.*\s+/",
    r"^chmod\b",
    r"^chown\b",
]

# Directories the agent must never write to
FORBIDDEN_WRITE_PATHS = [
    "/boot",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/proc",
    "/sys",
    "/dev",
]


def check_shell_command(command: str) -> tuple[SafetyLevel, Optional[str]]:
    """
    Returns (SafetyLevel, reason_or_None).
    Called before every shell tool execution.
    """
    cmd = command.strip()

    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            reason = f"matches forbidden pattern: {pattern}"
            logger.warning("BLOCKED command %r — %s", cmd, reason)
            return SafetyLevel.FORBIDDEN, reason

    for pattern in CONFIRM_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            reason = f"matches confirm pattern: {pattern}"
            logger.info("CONFIRM required for command %r — %s", cmd, reason)
            return SafetyLevel.CONFIRM, reason

    return SafetyLevel.SAFE, None


def check_file_write(path: str) -> tuple[SafetyLevel, Optional[str]]:
    """Check if writing to this path is allowed."""
    for forbidden in FORBIDDEN_WRITE_PATHS:
        if path.startswith(forbidden):
            reason = f"write to protected path: {forbidden}"
            logger.warning("BLOCKED file write to %r — %s", path, reason)
            return SafetyLevel.FORBIDDEN, reason
    return SafetyLevel.SAFE, None


def check_gpio_pin(pin: int, safe_pins: list[int]) -> tuple[SafetyLevel, Optional[str]]:
    """Only allow writes to explicitly whitelisted GPIO pins."""
    if pin not in safe_pins:
        reason = f"pin {pin} not in safe_pins whitelist"
        logger.warning("BLOCKED gpio write to pin %d — %s", pin, reason)
        return SafetyLevel.FORBIDDEN, reason
    return SafetyLevel.SAFE, None


class SafetyGuard:
    def __init__(self, safe_gpio_pins: list[int] = None,
                 confirm_wait_sec: int = 10,
                 dry_run: bool = False):
        self.safe_gpio_pins = safe_gpio_pins or []
        self.confirm_wait_sec = confirm_wait_sec
        self.dry_run = dry_run  # if True, log but never execute anything

    def approve_shell(self, command: str) -> tuple[bool, str]:
        """Returns (approved, message)."""
        if self.dry_run:
            return False, f"[dry-run] would execute: {command}"
        level, reason = check_shell_command(command)
        if level == SafetyLevel.FORBIDDEN:
            return False, f"FORBIDDEN: {reason}"
        if level == SafetyLevel.CONFIRM:
            import time
            logger.info("Waiting %ds before executing confirm-level command...",
                        self.confirm_wait_sec)
            time.sleep(self.confirm_wait_sec)
            return True, f"CONFIRMED after delay: {command}"
        return True, "ok"

    def approve_file_write(self, path: str) -> tuple[bool, str]:
        if self.dry_run:
            return False, f"[dry-run] would write: {path}"
        level, reason = check_file_write(path)
        if level == SafetyLevel.FORBIDDEN:
            return False, f"FORBIDDEN: {reason}"
        return True, "ok"

    def approve_gpio(self, pin: int) -> tuple[bool, str]:
        if self.dry_run:
            return False, f"[dry-run] would write gpio pin {pin}"
        level, reason = check_gpio_pin(pin, self.safe_gpio_pins)
        if level == SafetyLevel.FORBIDDEN:
            return False, f"FORBIDDEN: {reason}"
        return True, "ok"
