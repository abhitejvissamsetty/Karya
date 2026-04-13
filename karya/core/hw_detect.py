"""
hw_detect.py — detect hardware tier and set token/context budgets
No dependencies beyond stdlib + optional psutil.
"""

import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class HardwareTier:
    name: str           # "nano" | "micro" | "small" | "base"
    ram_gb: float
    max_ctx_tokens: int     # total context window to request from model
    history_tokens: int     # budget for conversation history
    tool_result_tokens: int # max tokens per tool result
    system_tokens: int      # budget for system prompt
    recommended_model: str
    cycle_interval_sec: int # how often agent loop runs


TIERS = [
    HardwareTier(
        name="nano",
        ram_gb=0,
        max_ctx_tokens=512,
        history_tokens=150,
        tool_result_tokens=100,
        system_tokens=200,
        recommended_model="tinyllama:1.1b-chat-v1-q4_K_M",
        cycle_interval_sec=60,
    ),
    HardwareTier(
        name="micro",
        ram_gb=1.5,
        max_ctx_tokens=2048,
        history_tokens=600,
        tool_result_tokens=300,
        system_tokens=400,
        recommended_model="qwen2.5:1.5b-instruct-q4_K_M",
        cycle_interval_sec=30,
    ),
    HardwareTier(
        name="small",
        ram_gb=3.5,
        max_ctx_tokens=4096,
        history_tokens=1500,
        tool_result_tokens=600,
        system_tokens=600,
        recommended_model="qwen2.5:3b-instruct-q4_K_M",
        cycle_interval_sec=20,
    ),
    HardwareTier(
        name="base",
        ram_gb=7,
        max_ctx_tokens=8192,
        history_tokens=4000,
        tool_result_tokens=1200,
        system_tokens=800,
        recommended_model="qwen2.5:7b-instruct-q4_K_M",
        cycle_interval_sec=15,
    ),
]


def _read_proc_meminfo() -> Optional[float]:
    """Read total RAM from /proc/meminfo (Linux/Pi)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / 1024 / 1024  # → GB
    except Exception:
        return None


def _read_psutil() -> Optional[float]:
    try:
        import psutil
        return psutil.virtual_memory().total / 1e9
    except ImportError:
        return None


def get_ram_gb() -> float:
    """Return total system RAM in GB. Falls back gracefully."""
    ram = _read_proc_meminfo() or _read_psutil()
    if ram is None:
        # macOS fallback
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
            ram = int(out.strip()) / 1e9
        except Exception:
            ram = 1.0  # safe default if nothing works
    return round(ram, 2)


def get_cpu_arch() -> str:
    machine = platform.machine().lower()
    if "aarch64" in machine or "arm64" in machine:
        return "arm64"
    if "armv" in machine:
        return "arm32"
    if "x86_64" in machine or "amd64" in machine:
        return "x86_64"
    return machine


def is_raspberry_pi() -> bool:
    try:
        with open("/proc/cpuinfo") as f:
            return "raspberry pi" in f.read().lower()
    except Exception:
        return False


def detect_tier(ram_gb: Optional[float] = None) -> HardwareTier:
    """Pick the right tier based on available RAM."""
    ram = ram_gb if ram_gb is not None else get_ram_gb()
    for tier in reversed(TIERS):
        if ram >= tier.ram_gb:
            return tier
    return TIERS[0]  # nano fallback


def print_hw_report():
    ram = get_ram_gb()
    arch = get_cpu_arch()
    tier = detect_tier(ram)
    pi = is_raspberry_pi()

    print("=" * 48)
    print("  karya hardware report")
    print("=" * 48)
    print(f"  platform   : {platform.system()} {arch}")
    print(f"  raspberry pi: {'yes' if pi else 'no'}")
    print(f"  RAM        : {ram:.1f} GB")
    print(f"  tier       : {tier.name}")
    print(f"  model rec  : {tier.recommended_model}")
    print(f"  ctx window : {tier.max_ctx_tokens} tokens")
    print(f"  cycle      : every {tier.cycle_interval_sec}s")
    print("=" * 48)
    return tier


if __name__ == "__main__":
    print_hw_report()
