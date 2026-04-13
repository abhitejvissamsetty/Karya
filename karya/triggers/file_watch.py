"""
triggers/file_watch.py — inotify-based file watcher trigger
Drop a .txt file in the watch directory → agent wakes and reads it.
This is the "dead drop" interface: works offline, no network, no terminal.

Useful for:
- Field deployment: write a task file to USB drive → agent picks it up
- Remote: mount NFS share → drop file → agent acts
- Cron on another machine: write a file → this agent responds

Falls back to polling if inotify is unavailable (Pi OS always has it).
"""

import os
import time
import logging
from pathlib import Path
from typing import Optional

from karya.triggers.base import BaseTrigger, TriggerCallback

logger = logging.getLogger("karya.triggers.file_watch")

DEFAULT_WATCH_DIR = Path.home() / ".karya" / "tasks"


class FileWatchTrigger(BaseTrigger):
    """
    Watches a directory for new .txt files.
    When one appears, reads it, fires the agent with the content,
    then moves it to a 'done' subdirectory.

    Usage:
        trigger = FileWatchTrigger(watch_dir="/tmp/tasks", callback=on_event)
        trigger.start()

    To send the agent a task:
        echo "check disk and clean if above 80%" > ~/.karya/tasks/cleanup.txt
    """

    def __init__(
        self,
        watch_dir: Optional[Path] = None,
        extensions: tuple = (".txt", ".task"),
        poll_interval: float = 2.0,
        callback: Optional[TriggerCallback] = None,
    ):
        super().__init__(name="file_watch", callback=callback)
        self.watch_dir = Path(watch_dir or DEFAULT_WATCH_DIR)
        self.done_dir = self.watch_dir / "done"
        self.extensions = extensions
        self.poll_interval = poll_interval
        self._seen: set = set()

    def _run(self):
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Watching %s for task files", self.watch_dir)

        # try inotify first, fall back to polling
        if self._try_inotify():
            return
        self._poll_loop()

    def _poll_loop(self):
        """Fallback: poll directory every N seconds."""
        while not self._stop_event.is_set():
            self._scan()
            self._stop_event.wait(timeout=self.poll_interval)

    def _scan(self):
        try:
            for entry in os.scandir(self.watch_dir):
                if entry.is_file() and entry.name not in self._seen:
                    ext = Path(entry.name).suffix.lower()
                    if ext in self.extensions:
                        self._seen.add(entry.name)
                        self._handle_file(Path(entry.path))
        except Exception as e:
            logger.warning("File scan error: %s", e)

    def _handle_file(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                return

            logger.info("New task file: %s", path.name)
            self.fire(
                reason=f"task file: {path.name}",
                data={"file": str(path), "content": content, "filename": path.name},
            )

            # move to done/
            done_path = self.done_dir / path.name
            # avoid collision
            if done_path.exists():
                done_path = self.done_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
            path.rename(done_path)
            logger.info("Task file moved to done/: %s", done_path.name)

        except Exception as e:
            logger.error("Error handling task file %s: %s", path, e)

    def _try_inotify(self) -> bool:
        """
        Try to use inotify via /proc/sys/fs/inotify (Linux only).
        Returns True if inotify loop ran, False to fall back to polling.
        """
        try:
            import select
            # Use inotifywait subprocess if available
            import subprocess
            result = subprocess.run(
                ["which", "inotifywait"], capture_output=True, text=True
            )
            if result.returncode != 0:
                return False

            proc = subprocess.Popen(
                [
                    "inotifywait",
                    "-m",           # monitor continuously
                    "-e", "close_write,moved_to",
                    "--format", "%f",
                    str(self.watch_dir),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            logger.info("Using inotifywait for %s", self.watch_dir)

            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if ready:
                    filename = proc.stdout.readline().strip()
                    if filename:
                        path = self.watch_dir / filename
                        if path.exists() and Path(filename).suffix.lower() in self.extensions:
                            if filename not in self._seen:
                                self._seen.add(filename)
                                self._handle_file(path)

            proc.terminate()
            return True

        except Exception:
            return False

    def drop_task(self, content: str, name: str = "task.txt"):
        """Helper: programmatically drop a task file (useful for testing)."""
        path = self.watch_dir / name
        path.write_text(content)
        logger.info("Task dropped: %s", path)
