"""
state.py — world state manager
Maintains a compact JSON snapshot of what the agent knows.
Survives reboots. No LLM needed to read or write it.
Max ~200 tokens when serialized — always fits in context.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


STATE_DIR = Path(os.environ.get("KARYA_HOME", Path.home() / ".karya"))
STATE_FILE = STATE_DIR / "state.json"
SESSION_LOG = STATE_DIR / "session.jsonl"
MAX_RECENT_ACTIONS = 6  # keep last N actions in hot state


@dataclass
class ActionRecord:
    timestamp: str
    trigger: str
    tool: str
    args: dict
    result: str      # truncated summary, max 80 chars
    success: bool


@dataclass
class WorldState:
    goals: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)    # key facts the agent tracks
    recent_actions: list[dict] = field(default_factory=list)
    pending_goals: list[str] = field(default_factory=list) # goals not yet achieved
    last_updated: str = ""
    cycle_count: int = 0


class StateManager:
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = state_dir or STATE_DIR
        self.state_file = self.state_dir / "state.json"
        self.session_log = self.state_dir / "session.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state: WorldState = self._load()

    def _load(self) -> WorldState:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                return WorldState(**data)
            except Exception:
                pass
        return WorldState()

    def save(self):
        self._state.last_updated = datetime.now().isoformat(timespec="seconds")
        with open(self.state_file, "w") as f:
            json.dump(asdict(self._state), f, indent=2)

    def get(self) -> WorldState:
        return self._state

    def set_goals(self, goals: list[str]):
        self._state.goals = goals
        self.save()

    def update_fact(self, key: str, value: Any):
        self._state.facts[key] = value
        self.save()

    def remove_fact(self, key: str):
        self._state.facts.pop(key, None)
        self.save()

    def record_action(self, trigger: str, tool: str, args: dict,
                      result: str, success: bool):
        record = ActionRecord(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            trigger=trigger,
            tool=tool,
            args=args,
            result=result[:80],  # hard truncate
            success=success,
        )
        self._state.recent_actions.append(asdict(record))
        # keep only last N
        if len(self._state.recent_actions) > MAX_RECENT_ACTIONS:
            self._state.recent_actions = self._state.recent_actions[-MAX_RECENT_ACTIONS:]
        self._state.cycle_count += 1
        self.save()
        # also append full record to session log
        self._append_session_log(asdict(record))

    def _append_session_log(self, record: dict):
        with open(self.session_log, "a") as f:
            f.write(json.dumps(record) + "\n")

    def to_prompt_block(self) -> str:
        """
        Serialize state into a compact string for injection into LLM prompt.
        Designed to stay under 300 tokens.
        """
        s = self._state
        lines = []

        if s.goals:
            lines.append("GOALS:")
            for g in s.goals:
                lines.append(f"  - {g}")

        if s.facts:
            lines.append("KNOWN_FACTS:")
            for k, v in s.facts.items():
                lines.append(f"  {k}: {v}")

        if s.recent_actions:
            lines.append("RECENT_ACTIONS (last {}):" .format(len(s.recent_actions)))
            for a in s.recent_actions[-3:]:  # only last 3 in prompt
                status = "ok" if a["success"] else "fail"
                lines.append(
                    f"  [{a['timestamp']}] {a['tool']}({a['args']}) → {a['result']} [{status}]"
                )

        if s.pending_goals:
            lines.append("PENDING:")
            for p in s.pending_goals:
                lines.append(f"  - {p}")

        lines.append(f"CYCLE: {s.cycle_count}")
        return "\n".join(lines)

    def mark_goal_pending(self, goal: str):
        if goal not in self._state.pending_goals:
            self._state.pending_goals.append(goal)
        self.save()

    def clear_pending_goal(self, goal: str):
        self._state.pending_goals = [
            g for g in self._state.pending_goals if g != goal
        ]
        self.save()
