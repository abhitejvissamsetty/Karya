"""
triggers/base.py — base class for all event triggers
A trigger watches for something to happen and fires the agent loop.
All triggers work offline — no network polling.
"""

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("karya.triggers")


@dataclass
class TriggerEvent:
    source: str          # which trigger fired: "gpio", "cron", "file", "serial", "threshold"
    reason: str          # human-readable: "pin 17 went LOW", "disk > 85%"
    data: dict = field(default_factory=dict)   # raw data payload
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# Callback type: receives a TriggerEvent, returns nothing
TriggerCallback = Callable[[TriggerEvent], None]


class BaseTrigger(ABC):
    """
    All triggers implement this interface.
    They run in background threads and call `callback` when fired.
    """

    def __init__(self, name: str, callback: Optional[TriggerCallback] = None):
        self.name = name
        self.callback = callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def set_callback(self, callback: TriggerCallback):
        self.callback = callback

    def fire(self, reason: str, data: dict = None):
        event = TriggerEvent(source=self.name, reason=reason, data=data or {})
        logger.info("Trigger fired: [%s] %s", self.name, reason)
        if self.callback:
            self.callback(event)

    def start(self):
        """Start the trigger in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"trigger-{self.name}")
        self._thread.start()
        logger.info("Trigger started: %s", self.name)

    def stop(self):
        """Signal the trigger thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Trigger stopped: %s", self.name)

    @abstractmethod
    def _run(self):
        """Override this — runs in background thread until _stop_event is set."""
        pass

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
