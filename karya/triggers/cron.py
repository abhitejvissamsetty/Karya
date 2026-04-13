"""
triggers/cron.py — schedule-based trigger
Fires the agent loop on a fixed interval. No crontab, no systemd timer.
Works entirely in-process, no external dependencies.
"""

import time
import logging
from typing import Optional
from karya.triggers.base import BaseTrigger, TriggerCallback

logger = logging.getLogger("karya.triggers.cron")


class CronTrigger(BaseTrigger):
    """
    Fires every `interval_seconds`. The simplest trigger —
    just wakes the agent on a heartbeat.

    Usage:
        trigger = CronTrigger(interval_seconds=30, callback=on_event)
        trigger.start()
    """

    def __init__(
        self,
        interval_seconds: int = 30,
        callback: Optional[TriggerCallback] = None,
        fire_immediately: bool = True,  # fire once right on start
    ):
        super().__init__(name="cron", callback=callback)
        self.interval = interval_seconds
        self.fire_immediately = fire_immediately

    def _run(self):
        if self.fire_immediately:
            self.fire(reason=f"startup (interval={self.interval}s)")

        while not self._stop_event.wait(timeout=self.interval):
            self.fire(
                reason=f"scheduled tick (every {self.interval}s)",
                data={"interval_sec": self.interval},
            )
