"""
core/priority.py — multi-goal priority ranking
Ranks goals by urgency so the agent tackles the most critical one first.
Pure rule-based. Zero LLM calls. Fast enough to run every cycle.

Priority is computed from:
1. Keyword urgency    — "critical", "emergency", "immediately" → highest
2. Metric proximity   — how close are we to breaching a threshold?
3. Trigger source     — threshold/gpio triggers outrank cron
4. Failure history    — goals that recently failed get boosted
5. Staleness          — goals not acted on in a long time get boosted

Final score 0–100. Highest score = act on this goal first.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("karya.priority")


# ── keyword urgency weights ───────────────────────────────────────────────────

URGENCY_KEYWORDS = {
    # score → keywords
    50: ["critical", "emergency", "immediately", "urgent", "danger",
         "fatal", "crash"],
    30: ["high", "important", "alert", "warn", "exceeded", "above",
         "fail", "error"],
    15: ["monitor", "check", "watch", "restart", "down", "stop",
         "kill", "track", "log", "notify"],
    5:  ["low", "background", "optional", "eventually", "when possible"],
}

# metric keywords that imply we should read current value from state
METRIC_KEYWORDS = {
    "disk":   "disk_used_pct",
    "memory": "mem_used_pct",
    "mem":    "mem_used_pct",
    "temp":   "cpu_temp_c",
    "cpu":    "cpu_used_pct",
}

# trigger source urgency bonus
TRIGGER_BONUS = {
    "threshold": 40,
    "gpio":      35,
    "serial":    25,
    "file_watch":20,
    "cron":       0,
    "manual":    10,
}


@dataclass
class ScoredGoal:
    goal: str
    score: float
    reasons: list = field(default_factory=list)

    def __lt__(self, other):
        return self.score > other.score   # higher score = higher priority


class GoalPrioritizer:
    """
    Ranks a list of goal strings by urgency.

    Usage:
        p = GoalPrioritizer()
        ranked = p.rank(
            goals=["keep disk below 85%", "restart nginx if down", "log metrics"],
            current_facts={"disk_used_pct": 91, "nginx_status": "stopped"},
            trigger_source="threshold:disk_used_pct",
            failed_goals=["restart nginx if down"],
        )
        # ranked[0] is the most urgent goal to work on now
    """

    def rank(
        self,
        goals: list,
        current_facts: Optional[dict] = None,
        trigger_source: str = "cron",
        failed_goals: Optional[list] = None,
        last_action_times: Optional[dict] = None,  # goal → unix timestamp
    ) -> list:
        """
        Returns goals sorted highest priority first.
        Each item is a ScoredGoal with score and reasons list.
        """
        facts = current_facts or {}
        failed = set(failed_goals or [])
        last_times = last_action_times or {}

        scored = []
        for goal in goals:
            sg = self._score(goal, facts, trigger_source, failed, last_times)
            scored.append(sg)

        scored.sort()   # uses __lt__ above: higher score first
        self._log_ranking(scored)
        return scored

    def _score(
        self,
        goal: str,
        facts: dict,
        trigger_source: str,
        failed: set,
        last_times: dict,
    ) -> ScoredGoal:
        score = 0.0
        reasons = []
        goal_lower = goal.lower()

        # ── 1. keyword urgency ────────────────────────────────────────────────
        for pts, keywords in URGENCY_KEYWORDS.items():
            for kw in keywords:
                if kw in goal_lower:
                    score += pts
                    reasons.append(f"+{pts} keyword '{kw}'")
                    break   # only one match per tier

        # ── 2. metric proximity ───────────────────────────────────────────────
        threshold_val = self._extract_threshold(goal_lower)
        metric_key = self._extract_metric_key(goal_lower)

        if metric_key and threshold_val is not None:
            current = facts.get(metric_key)
            if current is not None:
                try:
                    current = float(current)
                    threshold_val = float(threshold_val)
                    gap = abs(current - threshold_val)
                    operator = self._extract_operator(goal_lower)

                    # are we breaching the limit right now?
                    # "keep disk below 85%" → operator="<" means limit is 85,
                    # breach when current EXCEEDS it (current > 85)
                    # "alert if temp exceeds 75" → operator=">" means breach when current > 75
                    breaching = (
                        (operator == "<"  and current >= threshold_val) or
                        (operator == "<=" and current > threshold_val)  or
                        (operator == ">"  and current >= threshold_val) or
                        (operator == ">=" and current >= threshold_val)
                    )
                    if breaching:
                        score += 60
                        reasons.append(
                            f"+60 BREACHING: {metric_key}={current} {operator} {threshold_val}"
                        )
                    elif gap <= 5:
                        score += 30
                        reasons.append(
                            f"+30 near threshold: {metric_key}={current}, gap={gap:.1f}"
                        )
                    elif gap <= 15:
                        score += 10
                        reasons.append(
                            f"+10 approaching: {metric_key}={current}, gap={gap:.1f}"
                        )
                except (TypeError, ValueError):
                    pass

        # ── 3. trigger source bonus ───────────────────────────────────────────
        for src, bonus in TRIGGER_BONUS.items():
            if src in trigger_source.lower():
                # only apply bonus if goal is related to the triggering metric
                if self._trigger_matches_goal(trigger_source, goal_lower):
                    score += bonus
                    reasons.append(f"+{bonus} trigger match: {src}")
                break

        # ── 4. failure history boost ──────────────────────────────────────────
        if goal in failed:
            score += 25
            reasons.append("+25 recently failed — retry urgency")

        # ── 5. staleness boost ────────────────────────────────────────────────
        last_t = last_times.get(goal)
        if last_t is not None:
            age_min = (time.time() - last_t) / 60
            if age_min > 30:
                boost = min(20, age_min / 3)
                score += boost
                reasons.append(f"+{boost:.1f} stale: {age_min:.0f}min since last action")

        return ScoredGoal(goal=goal, score=round(score, 1), reasons=reasons)

    # ── parsing helpers ───────────────────────────────────────────────────────

    def _extract_threshold(self, text: str) -> Optional[float]:
        """Pull the numeric threshold from a goal string. e.g. '85%' → 85.0
        Requires % sign or threshold context words to avoid matching counts like 'every 5 minutes'."""
        # prefer numbers with % sign first
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
        if match:
            return float(match.group(1))
        # numbers next to threshold context words
        match = re.search(
            r'(?:above|below|exceed|over|under|greater|less|threshold|limit|than)\s+(\d+(?:\.\d+)?)',
            text
        )
        if match:
            return float(match.group(1))
        # bare numbers only if >= 10 (avoids "every 5 minutes", "3 retries")
        match = re.search(r'\b(\d{2,}(?:\.\d+)?)\b', text)
        if match:
            val = float(match.group(1))
            if val >= 10:
                return val
        return None

    def _extract_operator(self, text: str) -> str:
        """Detect comparison direction from goal wording."""
        if any(w in text for w in ["above", "exceed", "over", "greater", "higher", ">"]):
            return ">"
        if any(w in text for w in ["below", "under", "less", "lower", "<"]):
            return "<"
        return ">"  # default: threshold is upper limit

    def _extract_metric_key(self, text: str) -> Optional[str]:
        for keyword, fact_key in METRIC_KEYWORDS.items():
            if keyword in text:
                return fact_key
        return None

    def _trigger_matches_goal(self, trigger_source: str, goal_lower: str) -> bool:
        """True if the trigger that fired is related to this goal."""
        for keyword in METRIC_KEYWORDS:
            if keyword in trigger_source.lower() and keyword in goal_lower:
                return True
        # gpio trigger matches any gpio-related goal
        if "gpio" in trigger_source and "gpio" in goal_lower:
            return True
        # serial trigger matches any serial-related goal
        if "serial" in trigger_source and "serial" in goal_lower:
            return True
        # file trigger matches any goal (it's a direct command)
        if "file_watch" in trigger_source:
            return True
        return False

    def _log_ranking(self, scored: list):
        logger.debug("Goal ranking:")
        for i, sg in enumerate(scored):
            logger.debug("  #%d [%.0f] %s — %s",
                         i + 1, sg.score, sg.goal[:50], "; ".join(sg.reasons[:2]))


def build_priority_prompt(ranked_goals: list, facts: dict) -> str:
    """
    Build the prioritized goal block for injection into the system prompt.
    Most urgent goal is listed first with its score and reasons.
    """
    lines = ["GOALS (highest priority first):"]
    for i, sg in enumerate(ranked_goals):
        urgency = "URGENT" if sg.score >= 60 else "HIGH" if sg.score >= 30 else "normal"
        prefix = ">>>" if i == 0 else "   "
        lines.append(f"  {prefix} [{urgency}] {sg.goal}")
        if i == 0 and sg.reasons:
            lines.append(f"       reason: {', '.join(sg.reasons[:2])}")
    return "\n".join(lines)
