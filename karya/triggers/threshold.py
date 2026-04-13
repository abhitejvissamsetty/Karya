"""
triggers/threshold.py — metric threshold trigger
Fires when a system metric crosses a defined limit.
No polling waste — checks on its own interval, fires only when threshold crossed.
Supports hysteresis to avoid thrashing (won't re-fire until metric recovers).

Examples:
    ThresholdTrigger("disk_used_pct", ">", 85, check_every=60)
    ThresholdTrigger("cpu_temp_c",    ">", 75, check_every=10)
    ThresholdTrigger("mem_used_pct",  ">", 90, check_every=30)
"""

import json
import logging
import shutil
import time
from typing import Callable, Optional, Union

from karya.triggers.base import BaseTrigger, TriggerCallback

logger = logging.getLogger("karya.triggers.threshold")

Number = Union[int, float]


def _read_metric(metric: str) -> Optional[Number]:
    """Read a system metric. Returns None if unavailable."""

    if metric == "disk_used_pct":
        try:
            total, used, _ = shutil.disk_usage("/")
            return round(used / total * 100, 1)
        except Exception:
            return None

    if metric == "mem_used_pct":
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith(("MemTotal", "MemAvailable")):
                        k, v = line.split(":")
                        info[k.strip()] = int(v.split()[0])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            if total == 0:
                return None
            return round((total - avail) / total * 100, 1)
        except Exception:
            return None

    if metric == "cpu_temp_c":
        paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]
        for p in paths:
            try:
                with open(p) as f:
                    return int(f.read().strip()) / 1000.0
            except Exception:
                continue
        return None

    if metric == "cpu_used_pct":
        try:
            def _read():
                with open("/proc/stat") as f:
                    vals = list(map(int, f.readline().split()[1:]))
                return vals[3], sum(vals)  # idle, total

            idle1, total1 = _read()
            time.sleep(0.2)
            idle2, total2 = _read()
            diff_total = total2 - total1
            diff_idle = idle2 - idle1
            if diff_total == 0:
                return None
            return round(100 - (diff_idle / diff_total * 100), 1)
        except Exception:
            return None

    return None


class ThresholdTrigger(BaseTrigger):
    """
    Monitors a metric and fires when it crosses a threshold.

    Args:
        metric:        one of: disk_used_pct, mem_used_pct, cpu_temp_c, cpu_used_pct
        operator:      ">" | "<" | ">=" | "<="
        threshold:     numeric value to compare against
        check_every:   seconds between checks
        hysteresis:    how much the metric must recover before re-firing
                       (prevents constant firing when value hovers at threshold)
        fire_on_clear: if True, also fires when metric drops back below threshold

    Usage:
        t = ThresholdTrigger("disk_used_pct", ">", 85, check_every=60)
        t.set_callback(on_event)
        t.start()
    """

    OPERATORS = {
        ">":  lambda v, t: v > t,
        "<":  lambda v, t: v < t,
        ">=": lambda v, t: v >= t,
        "<=": lambda v, t: v <= t,
    }

    def __init__(
        self,
        metric: str,
        operator: str,
        threshold: Number,
        check_every: int = 30,
        hysteresis: Number = 2,
        fire_on_clear: bool = False,
        callback: Optional[TriggerCallback] = None,
    ):
        super().__init__(name=f"threshold:{metric}", callback=callback)
        self.metric = metric
        self.operator = operator
        self.threshold = threshold
        self.check_every = check_every
        self.hysteresis = hysteresis
        self.fire_on_clear = fire_on_clear

        if operator not in self.OPERATORS:
            raise ValueError(f"Unknown operator: {operator}. Use: {list(self.OPERATORS)}")

        self._compare = self.OPERATORS[operator]
        self._in_alarm = False   # currently above threshold?

    def _run(self):
        logger.info(
            "Monitoring %s %s %s (check every %ds, hysteresis %s)",
            self.metric, self.operator, self.threshold,
            self.check_every, self.hysteresis,
        )

        while not self._stop_event.wait(timeout=self.check_every):
            value = _read_metric(self.metric)
            if value is None:
                logger.debug("Metric %s unavailable", self.metric)
                continue

            breached = self._compare(value, self.threshold)

            if breached and not self._in_alarm:
                # just crossed threshold → fire
                self._in_alarm = True
                self.fire(
                    reason=f"{self.metric} {self.operator} {self.threshold} (value={value})",
                    data={
                        "metric": self.metric,
                        "value": value,
                        "threshold": self.threshold,
                        "operator": self.operator,
                    },
                )

            elif not breached and self._in_alarm:
                # check recovery with hysteresis
                recovered = self._check_recovery(value)
                if recovered:
                    self._in_alarm = False
                    if self.fire_on_clear:
                        self.fire(
                            reason=f"{self.metric} recovered to {value} (threshold={self.threshold})",
                            data={"metric": self.metric, "value": value, "cleared": True},
                        )

    def _check_recovery(self, value: Number) -> bool:
        """True if metric has moved far enough from threshold to reset alarm."""
        if self.operator in (">", ">="):
            return value <= (self.threshold - self.hysteresis)
        if self.operator in ("<", "<="):
            return value >= (self.threshold + self.hysteresis)
        return True
