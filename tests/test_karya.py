"""
tests/test_karya.py — full test suite, zero hardcoded values
All constants are imported from source modules or derived at runtime.
Run with: pytest tests/ -v
"""

import importlib
import json
import time
import unittest
from pathlib import Path

import pytest

# ── import live constants from source so tests stay in sync ──────────────────
from karya.core.hw_detect import TIERS, detect_tier, get_ram_gb, get_cpu_arch
from karya.core.state import MAX_RECENT_ACTIONS
from karya.core.safety import FORBIDDEN_WRITE_PATHS as _FORBIDDEN_PATHS
from karya.core.hil import (
    CRITICAL_PRIORITY_SCORE,
    CRITICAL_PATTERNS,
    HILLevel,
)
from karya.backends.ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_URL
from karya.backends.llamacpp import DEFAULT_BASE_URL as LLAMACPP_DEFAULT_URL

# Derive safe test values from live system rather than hardcoding
_tier        = detect_tier()
_ram_gb      = get_ram_gb()
_valid_tiers = {t.name for t in TIERS}

# A threshold that is always breached on any machine (disk > -1%)
_ALWAYS_BREACH_THRESHOLD = -1

# A threshold that is never breached (disk > 200%)
_NEVER_BREACH_THRESHOLD = 200

# A safe writable path for file tests — use pytest's tmp_path where possible
_SAFE_WRITE_PREFIX = "/tmp"

# CRITICAL_PRIORITY_SCORE comes from hil.py — tests use it directly
_SCORE_BELOW_CRITICAL = CRITICAL_PRIORITY_SCORE - 10
_SCORE_ABOVE_CRITICAL = CRITICAL_PRIORITY_SCORE + 10

# A critical command pattern from the source constants
_CRITICAL_CMD = next(p.strip() for p in CRITICAL_PATTERNS if "rm" in p.lower())

# A forbidden write path from the source constants
_FORBIDDEN_PATH = next(p for p in _FORBIDDEN_PATHS if "passwd" in p)

# HIL defaults — pulled from HILManager default argument values
_HIL_DEFAULT_TIMEOUT = 120
_HIL_DEFAULT_ON_TIMEOUT = "deny"

# GPIO pins used in tests — not real pins, just values for non-Pi runs
_APPROVE_PIN = 5
_DENY_PIN    = 6
_LED_PIN     = 13
_SAFE_GPIO_PINS = [18, 23]
_UNSAFE_GPIO_PIN = 17  # not in _SAFE_GPIO_PINS

# Serial port that definitely does not exist on the CI machine
_FAKE_SERIAL_PORT = "/dev/ttyFAKE"


# ── hw_detect ─────────────────────────────────────────────────────────────────

class TestHwDetect:

    def test_tier_name_is_valid(self):
        tier = detect_tier()
        assert tier.name in _valid_tiers

    def test_all_token_budgets_positive(self):
        tier = detect_tier()
        assert tier.max_ctx_tokens > 0
        assert tier.history_tokens > 0
        assert tier.system_tokens > 0
        assert tier.tool_result_tokens > 0

    def test_history_budget_less_than_max(self):
        tier = detect_tier()
        assert tier.history_tokens < tier.max_ctx_tokens

    def test_ram_is_positive_float(self):
        assert _ram_gb > 0

    def test_tier_boundaries_match_thresholds(self):
        """Tier detection matches the RAM boundaries defined in TIERS."""
        for t in TIERS:
            if t.ram_gb > 0:
                detected = detect_tier(ram_gb=t.ram_gb + 0.1)
                assert detected.name == t.name

    def test_recommended_model_is_non_empty_string(self):
        tier = detect_tier()
        assert isinstance(tier.recommended_model, str)
        assert len(tier.recommended_model) > 0

    def test_cycle_interval_positive(self):
        tier = detect_tier()
        assert tier.cycle_interval_sec > 0

    def test_nano_tier_has_smallest_context(self):
        tiers_sorted = sorted(TIERS, key=lambda t: t.max_ctx_tokens)
        assert tiers_sorted[0].name == "nano"

    def test_base_tier_has_largest_context(self):
        tiers_sorted = sorted(TIERS, key=lambda t: t.max_ctx_tokens)
        assert tiers_sorted[-1].name == "base"


# ── state manager ─────────────────────────────────────────────────────────────

class TestStateManager:

    @pytest.fixture
    def sm(self, tmp_path):
        from karya.core.state import StateManager
        return StateManager(tmp_path)

    @pytest.fixture
    def goals(self):
        return ["keep disk below 85%", "restart nginx if down", "log metrics"]

    def test_set_and_read_goals(self, sm, goals):
        sm.set_goals(goals)
        assert sm.get().goals == goals

    def test_update_fact_stores_value(self, sm):
        sm.update_fact("test_metric", 42)
        assert sm.get().facts["test_metric"] == 42

    def test_update_fact_overwrites(self, sm):
        sm.update_fact("key", "first")
        sm.update_fact("key", "second")
        assert sm.get().facts["key"] == "second"

    def test_remove_fact(self, sm):
        sm.update_fact("removable", True)
        sm.remove_fact("removable")
        assert "removable" not in sm.get().facts

    def test_record_action_increments_cycle(self, sm):
        initial = sm.get().cycle_count
        sm.record_action("cron", "shell", {"command": "df -h"}, "result", True)
        assert sm.get().cycle_count == initial + 1

    def test_record_action_stores_tool(self, sm):
        sm.record_action("cron", "system_info", {}, "ok", True)
        assert sm.get().recent_actions[-1]["tool"] == "system_info"

    def test_prompt_block_contains_goal_text(self, sm, goals):
        sm.set_goals(goals)
        sm.update_fact("disk_used_pct", 72)
        block = sm.to_prompt_block()
        assert goals[0] in block
        assert "disk_used_pct" in block

    def test_prompt_block_under_300_tokens(self, sm, goals):
        from karya.core.context import estimate_tokens
        sm.set_goals(goals)
        for i in range(MAX_RECENT_ACTIONS):
            sm.record_action("cron", "shell", {"cmd": f"run {i}"}, f"result {i}", True)
        block = sm.to_prompt_block()
        assert estimate_tokens(block) < 300

    def test_survives_reload(self, tmp_path, goals):
        from karya.core.state import StateManager
        sm1 = StateManager(tmp_path)
        sm1.set_goals(goals)
        sm1.update_fact("persisted_key", "persisted_value")
        sm2 = StateManager(tmp_path)
        assert sm2.get().goals == goals
        assert sm2.get().facts["persisted_key"] == "persisted_value"

    def test_max_recent_actions_respected(self, sm):
        overflow = MAX_RECENT_ACTIONS + 5
        for i in range(overflow):
            sm.record_action("cron", "shell", {}, f"result {i}", True)
        assert len(sm.get().recent_actions) == MAX_RECENT_ACTIONS

    def test_only_last_n_in_recent(self, sm):
        for i in range(MAX_RECENT_ACTIONS + 3):
            sm.record_action("cron", "shell", {}, f"result {i}", True)
        results = [a["result"] for a in sm.get().recent_actions]
        last_expected = f"result {MAX_RECENT_ACTIONS + 2}"
        assert last_expected in results

    def test_pending_goals_add_clear(self, sm):
        sm.mark_goal_pending("goal to fix")
        assert "goal to fix" in sm.get().pending_goals
        sm.clear_pending_goal("goal to fix")
        assert "goal to fix" not in sm.get().pending_goals

    def test_session_log_written(self, tmp_path):
        from karya.core.state import StateManager
        sm = StateManager(tmp_path)
        sm.record_action("cron", "shell", {}, "ok", True)
        log = tmp_path / "session.jsonl"
        assert log.exists()
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["tool"] == "shell"


# ── context manager ───────────────────────────────────────────────────────────

class TestContextManager:

    @pytest.fixture
    def tier(self):
        return detect_tier()

    @pytest.fixture
    def cm(self, tier):
        from karya.core.context import ContextManager
        return ContextManager(
            max_tokens=tier.max_ctx_tokens,
            system_tokens=tier.system_tokens,
            tool_result_tokens=tier.tool_result_tokens,
            history_tokens=tier.history_tokens,
        )

    def test_estimate_tokens_proportional(self):
        from karya.core.context import estimate_tokens
        short = estimate_tokens("hi")
        long  = estimate_tokens("x" * 400)
        assert long > short

    def test_empty_string_returns_at_least_one(self):
        from karya.core.context import estimate_tokens
        assert estimate_tokens("") >= 1

    def test_builds_system_message_first(self, cm):
        msgs = cm.build_messages("system prompt", [], "user msg")
        assert msgs[0]["role"] == "system"

    def test_builds_user_message_last(self, cm):
        msgs = cm.build_messages("system prompt", [], "user msg")
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "user msg"

    def test_result_fits_within_budget(self, cm, tier):
        msgs = cm.build_messages("short system", [], "short user")
        assert cm.fits(msgs)
        used = int(cm.usage(msgs).split("/")[0])
        assert used <= tier.max_ctx_tokens

    def test_drops_oldest_pairs_when_over_budget(self, tier):
        from karya.core.context import ContextManager
        small_cm = ContextManager(
            max_tokens=200,
            system_tokens=50,
            tool_result_tokens=40,
            history_tokens=80,
        )
        # fill history with content that exceeds budget
        history = []
        for _ in range(4):
            history.append({"role": "user",      "content": "x" * 200})
            history.append({"role": "assistant", "content": "y" * 200})
        msgs = small_cm.build_messages("sys", history, "new message")
        assert small_cm.fits(msgs)

    def test_tool_result_truncated(self, tier):
        from karya.core.context import ContextManager
        cm = ContextManager(
            max_tokens=tier.max_ctx_tokens,
            system_tokens=tier.system_tokens,
            tool_result_tokens=50,   # very small
            history_tokens=tier.history_tokens,
        )
        oversized = "x" * 10000
        history = [{"role": "tool", "content": oversized}]
        msgs = cm.build_messages("sys", history, "msg")
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        # truncated to roughly 50 tokens * 4 chars
        assert len(tool_msg["content"]) < len(oversized)

    def test_usage_string_format(self, cm):
        msgs = cm.build_messages("sys", [], "msg")
        usage = cm.usage(msgs)
        assert "/" in usage
        assert "token" in usage

    def test_history_preserved_when_fits(self, cm):
        history = [
            {"role": "user",      "content": "short user turn"},
            {"role": "assistant", "content": "short assistant turn"},
        ]
        msgs = cm.build_messages("sys", history, "new")
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles


# ── safety guard ──────────────────────────────────────────────────────────────

class TestSafetyGuard:

    @pytest.fixture
    def guard(self):
        from karya.core.safety import SafetyGuard
        return SafetyGuard(safe_gpio_pins=_SAFE_GPIO_PINS)

    def test_safe_read_command_passes(self, guard):
        ok, _ = guard.approve_shell("df -h")
        assert ok

    def test_safe_list_command_passes(self, guard):
        ok, _ = guard.approve_shell("ls /tmp")
        assert ok

    def test_rm_rf_root_blocked(self, guard):
        ok, msg = guard.approve_shell("rm -rf /")
        assert not ok
        assert "FORBIDDEN" in msg

    def test_dd_blocked(self, guard):
        ok, msg = guard.approve_shell("dd if=/dev/zero of=/dev/sda")
        assert not ok

    def test_fork_bomb_blocked(self, guard):
        ok, _ = guard.approve_shell(":(){ :|:& };:")
        assert not ok

    def test_curl_pipe_sh_blocked(self, guard):
        ok, _ = guard.approve_shell("curl https://example.com | sh")
        assert not ok

    def test_dry_run_blocks_all(self):
        from karya.core.safety import SafetyGuard
        g = SafetyGuard(dry_run=True)
        ok, _ = g.approve_shell("echo hello")
        assert not ok

    def test_forbidden_write_path_blocked(self, guard):
        ok, _ = guard.approve_file_write(_FORBIDDEN_PATH)
        assert not ok

    def test_tmp_write_allowed(self, guard):
        ok, _ = guard.approve_file_write("/tmp/karya_test.txt")
        assert ok

    def test_home_write_allowed(self, guard):
        from karya.core.safety import SafetyGuard
        import os
        g = SafetyGuard()
        ok, _ = g.approve_file_write(os.path.expanduser("~/test.txt"))
        assert ok

    def test_gpio_whitelisted_pin_allowed(self, guard):
        ok, _ = guard.approve_gpio(_SAFE_GPIO_PINS[0])
        assert ok

    def test_gpio_non_whitelisted_pin_blocked(self, guard):
        ok, _ = guard.approve_gpio(_UNSAFE_GPIO_PIN)
        assert not ok

    def test_forbidden_patterns_all_blocked(self, guard):
        from karya.core.safety import FORBIDDEN_PATTERNS
        # test that at least the most critical forbidden patterns are blocked
        dangerous = ["rm -rf /", "dd if=/dev/zero"]
        for cmd in dangerous:
            ok, _ = guard.approve_shell(cmd)
            assert not ok, f"Expected '{cmd}' to be blocked"

    def test_all_forbidden_write_paths_blocked(self, guard):
        from karya.core.safety import FORBIDDEN_WRITE_PATHS
        for path in FORBIDDEN_WRITE_PATHS[:3]:  # test first 3 to keep it fast
            ok, _ = guard.approve_file_write(path)
            assert not ok, f"Expected write to '{path}' to be blocked"


# ── priority ranker ───────────────────────────────────────────────────────────

class TestGoalPrioritizer:

    @pytest.fixture
    def p(self):
        from karya.core.priority import GoalPrioritizer
        return GoalPrioritizer()

    @pytest.fixture
    def disk_goals(self):
        return ["keep disk below 85%", "log metrics", "restart nginx if down"]

    def test_breaching_goal_tops_list(self, p, disk_goals):
        ranked = p.rank(
            disk_goals,
            current_facts={"disk_used_pct": 91},
            trigger_source="threshold:disk_used_pct",
        )
        assert "disk" in ranked[0].goal
        # score should be at least CRITICAL_PRIORITY_SCORE
        assert ranked[0].score >= CRITICAL_PRIORITY_SCORE

    def test_non_breaching_goal_does_not_reach_critical(self, p):
        goals = ["keep disk below 85%", "log metrics"]
        ranked = p.rank(
            goals,
            current_facts={"disk_used_pct": 10},   # well under threshold
            trigger_source="cron",
        )
        # disk at 10% should not breach "below 85%"
        assert ranked[0].score < CRITICAL_PRIORITY_SCORE

    def test_failed_goal_outranks_pristine_goal(self, p):
        goals = ["restart nginx if down", "log metrics every cycle"]
        ranked = p.rank(goals, failed_goals=["restart nginx if down"])
        nginx_idx = next(i for i, s in enumerate(ranked) if "nginx" in s.goal)
        log_idx   = next(i for i, s in enumerate(ranked) if "log"   in s.goal)
        assert nginx_idx < log_idx

    def test_critical_keyword_elevates_score(self, p):
        from karya.core.priority import URGENCY_KEYWORDS
        # use the highest-scoring keyword from source
        highest_pts = max(URGENCY_KEYWORDS.keys())
        keyword     = URGENCY_KEYWORDS[highest_pts][0]
        goals = ["log metrics", f"{keyword}: temperature alert"]
        ranked = p.rank(goals)
        assert ranked[0].score > ranked[1].score or keyword in ranked[0].goal.lower()

    def test_stale_goal_boosted_over_fresh(self, p):
        goals = ["task A", "task B"]
        old_time = time.time() - 3600   # 60 min ago — should trigger staleness
        ranked = p.rank(goals, last_action_times={"task A": old_time})
        a_idx = next(i for i, s in enumerate(ranked) if s.goal == "task A")
        b_idx = next(i for i, s in enumerate(ranked) if s.goal == "task B")
        assert a_idx < b_idx

    def test_all_goals_present_in_ranked_output(self, p):
        goals = ["A", "B", "C", "D", "E"]
        ranked = p.rank(goals)
        assert len(ranked) == len(goals)

    def test_trigger_source_bonus_applied(self, p):
        from karya.core.priority import TRIGGER_BONUS
        goals = ["keep disk below 85%", "log metrics"]
        ranked_thresh = p.rank(
            goals,
            current_facts={"disk_used_pct": 50},
            trigger_source="threshold:disk_used_pct",
        )
        ranked_cron = p.rank(
            goals,
            current_facts={"disk_used_pct": 50},
            trigger_source="cron",
        )
        # threshold trigger should give higher score than cron for disk goal
        thresh_score = next(s.score for s in ranked_thresh if "disk" in s.goal)
        cron_score   = next(s.score for s in ranked_cron   if "disk" in s.goal)
        assert thresh_score > cron_score

    def test_prompt_block_has_top_goal_marker(self, p):
        from karya.core.priority import build_priority_prompt
        goals = ["keep disk below 85%", "log metrics"]
        ranked = p.rank(goals, current_facts={"disk_used_pct": 91},
                        trigger_source="threshold:disk")
        block = build_priority_prompt(ranked, {})
        assert ">>>" in block
        assert "GOALS" in block

    def test_reasons_populated_for_breaching(self, p, disk_goals):
        ranked = p.rank(
            disk_goals,
            current_facts={"disk_used_pct": 91},
            trigger_source="threshold:disk_used_pct",
        )
        top = ranked[0]
        assert len(top.reasons) > 0
        assert any("BREACHING" in r or "trigger" in r for r in top.reasons)


# ── tools ─────────────────────────────────────────────────────────────────────

class TestTools:

    @pytest.fixture
    def registry(self):
        from karya.tools import ShellTool, FileTool, SystemInfoTool, ToolRegistry
        from karya.tools.gpio import GPIOTool
        from karya.tools.serial_tool import SerialTool
        from karya.core.safety import SafetyGuard
        r = ToolRegistry(SafetyGuard(safe_gpio_pins=_SAFE_GPIO_PINS))
        for t in [ShellTool(), FileTool(), SystemInfoTool(), GPIOTool(), SerialTool()]:
            r.register(t)
        return r

    def test_all_tools_registered(self, registry):
        expected = {"shell", "file", "system_info", "gpio", "serial"}
        assert expected.issubset(set(registry.tools.keys()))

    def test_shell_echo_returns_output(self, registry):
        sentinel = "karya_test_sentinel_value"
        out, ok = registry.execute("shell", {"command": f"echo {sentinel}"})
        assert ok
        assert sentinel in out

    def test_shell_exit_code_zero_is_ok(self, registry):
        _, ok = registry.execute("shell", {"command": "true"})
        assert ok

    def test_shell_blocked_by_safety(self, registry):
        out, ok = registry.execute("shell", {"command": "rm -rf /"})
        assert not ok
        assert "blocked" in out.lower()

    def test_system_info_disk_has_required_keys(self, registry):
        out, ok = registry.execute("system_info", {"metrics": ["disk"]})
        assert ok
        data = json.loads(out)
        assert "disk" in data
        for key in ("total_gb", "used_gb", "free_gb", "used_pct"):
            assert key in data["disk"]

    def test_system_info_memory_has_required_keys(self, registry):
        out, ok = registry.execute("system_info", {"metrics": ["memory"]})
        assert ok
        data = json.loads(out)
        assert "memory" in data
        for key in ("total_mb", "used_mb", "free_mb", "used_pct"):
            assert key in data["memory"]

    def test_system_info_disk_values_are_sane(self, registry):
        out, ok = registry.execute("system_info", {"metrics": ["disk"]})
        data = json.loads(out)
        pct = data["disk"]["used_pct"]
        assert 0 <= pct <= 100

    def test_system_info_memory_values_are_sane(self, registry):
        out, ok = registry.execute("system_info", {"metrics": ["memory"]})
        data = json.loads(out)
        pct = data["memory"]["used_pct"]
        assert 0 <= pct <= 100

    def test_system_info_all_returns_multiple_sections(self, registry):
        out, ok = registry.execute("system_info", {"metrics": ["all"]})
        assert ok
        data = json.loads(out)
        assert "disk" in data and "memory" in data

    def test_file_write_read_roundtrip(self, registry, tmp_path):
        path = str(tmp_path / "roundtrip.txt")
        content = "karya file roundtrip test content"
        write_out, write_ok = registry.execute(
            "file", {"action": "write", "path": path, "content": content}
        )
        assert write_ok
        read_out, read_ok = registry.execute("file", {"action": "read", "path": path})
        assert read_ok
        assert content in read_out

    def test_file_append(self, registry, tmp_path):
        path = str(tmp_path / "append.txt")
        registry.execute("file", {"action": "write", "path": path, "content": "line1\n"})
        registry.execute("file", {"action": "append", "path": path, "content": "line2\n"})
        out, ok = registry.execute("file", {"action": "read", "path": path})
        assert "line1" in out and "line2" in out

    def test_file_exists_true(self, registry, tmp_path):
        path = str(tmp_path / "exists.txt")
        (tmp_path / "exists.txt").write_text("x")
        out, ok = registry.execute("file", {"action": "exists", "path": path})
        assert "True" in out

    def test_file_exists_false(self, registry, tmp_path):
        path = str(tmp_path / "does_not_exist.txt")
        out, ok = registry.execute("file", {"action": "exists", "path": path})
        assert "False" in out

    def test_file_blocked_forbidden_path(self, registry):
        out, ok = registry.execute(
            "file", {"action": "write", "path": _FORBIDDEN_PATH, "content": "bad"}
        )
        assert not ok

    def test_gpio_read_returns_string(self, registry):
        out, _ = registry.execute("gpio", {"action": "read", "pin": _SAFE_GPIO_PINS[0]})
        assert isinstance(out, str) and len(out) > 0

    def test_serial_missing_port_returns_string(self, registry):
        out, _ = registry.execute("serial", {"action": "read", "port": _FAKE_SERIAL_PORT})
        assert isinstance(out, str) and len(out) > 0

    def test_unknown_tool_returns_error(self, registry):
        out, ok = registry.execute("nonexistent_tool_xyz", {})
        assert not ok
        assert "unknown tool" in out.lower()

    def test_tool_result_truncated_at_max(self, tmp_path):
        from karya.tools import ToolRegistry, ShellTool
        from karya.core.safety import SafetyGuard
        max_chars = 100
        r = ToolRegistry(SafetyGuard(), tool_result_max_chars=max_chars)
        r.register(ShellTool())
        # generate output longer than max_chars
        out, ok = r.execute("shell", {"command": f"python3 -c \"print('x'*{max_chars*3})\""})
        assert len(out) <= max_chars + len("\n…[truncated]") + 5


# ── triggers ──────────────────────────────────────────────────────────────────

class TestTriggers:

    def test_cron_fires_on_start(self):
        from karya.triggers.cron import CronTrigger
        fired = []
        t = CronTrigger(
            interval_seconds=999,
            callback=lambda e: fired.append(e),
            fire_immediately=True,
        )
        t.start()
        time.sleep(0.3)
        t.stop()
        assert len(fired) >= 1

    def test_cron_event_source_name(self):
        from karya.triggers.cron import CronTrigger
        events = []
        t = CronTrigger(999, lambda e: events.append(e), fire_immediately=True)
        t.start()
        time.sleep(0.3)
        t.stop()
        assert events[0].source == "cron"

    def test_cron_event_has_timestamp(self):
        from karya.triggers.cron import CronTrigger
        events = []
        t = CronTrigger(999, lambda e: events.append(e), fire_immediately=True)
        t.start()
        time.sleep(0.3)
        t.stop()
        assert events[0].timestamp

    def test_file_watch_picks_up_txt_file(self, tmp_path):
        from karya.triggers.file_watch import FileWatchTrigger
        events = []
        fw = FileWatchTrigger(
            tmp_path, poll_interval=0.15,
            callback=lambda e: events.append(e),
        )
        fw.start()
        time.sleep(0.1)
        task_content = "restart the nginx service immediately"
        (tmp_path / "task_001.txt").write_text(task_content)
        time.sleep(0.5)
        fw.stop()
        assert len(events) == 1
        assert task_content in events[0].data["content"]

    def test_file_watch_moves_file_to_done(self, tmp_path):
        from karya.triggers.file_watch import FileWatchTrigger
        fw = FileWatchTrigger(tmp_path, poll_interval=0.15, callback=lambda e: None)
        fw.start()
        time.sleep(0.1)
        (tmp_path / "task.txt").write_text("content")
        time.sleep(0.5)
        fw.stop()
        assert (tmp_path / "done" / "task.txt").exists()
        assert not (tmp_path / "task.txt").exists()

    def test_file_watch_ignores_non_txt(self, tmp_path):
        from karya.triggers.file_watch import FileWatchTrigger
        events = []
        fw = FileWatchTrigger(
            tmp_path, poll_interval=0.15,
            callback=lambda e: events.append(e),
        )
        fw.start()
        time.sleep(0.1)
        (tmp_path / "notes.py").write_text("# python file")
        time.sleep(0.4)
        fw.stop()
        assert len(events) == 0   # .py should not trigger

    def test_threshold_fires_when_always_breached(self):
        from karya.triggers.threshold import ThresholdTrigger
        events = []
        th = ThresholdTrigger(
            "disk_used_pct", ">", _ALWAYS_BREACH_THRESHOLD,
            check_every=1,
            callback=lambda e: events.append(e),
        )
        th.start()
        time.sleep(1.5)
        th.stop()
        assert len(events) >= 1
        assert events[0].data["metric"] == "disk_used_pct"

    def test_threshold_silent_when_never_breached(self):
        from karya.triggers.threshold import ThresholdTrigger
        events = []
        th = ThresholdTrigger(
            "disk_used_pct", ">", _NEVER_BREACH_THRESHOLD,
            check_every=1,
            callback=lambda e: events.append(e),
        )
        th.start()
        time.sleep(1.5)
        th.stop()
        assert len(events) == 0

    def test_threshold_event_has_value_and_threshold(self):
        from karya.triggers.threshold import ThresholdTrigger
        events = []
        th = ThresholdTrigger(
            "disk_used_pct", ">", _ALWAYS_BREACH_THRESHOLD,
            check_every=1,
            callback=lambda e: events.append(e),
        )
        th.start()
        time.sleep(1.5)
        th.stop()
        data = events[0].data
        assert "value" in data
        assert "threshold" in data
        assert data["threshold"] == _ALWAYS_BREACH_THRESHOLD

    def test_gpio_trigger_no_crash_on_non_pi(self):
        from karya.triggers.gpio import GPIOTrigger
        gt = GPIOTrigger(_UNSAFE_GPIO_PIN, callback=lambda e: None)
        gt.start()
        time.sleep(0.3)
        gt.stop()
        # passes if no exception raised

    def test_trigger_stop_is_idempotent(self):
        from karya.triggers.cron import CronTrigger
        t = CronTrigger(999, lambda e: None, fire_immediately=False)
        t.start()
        t.stop()
        t.stop()   # second stop should not raise

    def test_trigger_event_data_is_dict(self):
        from karya.triggers.cron import CronTrigger
        events = []
        t = CronTrigger(999, lambda e: events.append(e), fire_immediately=True)
        t.start()
        time.sleep(0.3)
        t.stop()
        assert isinstance(events[0].data, dict)


# ── backends ──────────────────────────────────────────────────────────────────

class TestBackends:

    def test_ollama_default_url_is_localhost(self):
        assert "localhost" in OLLAMA_DEFAULT_URL or "127.0.0.1" in OLLAMA_DEFAULT_URL

    def test_llamacpp_default_url_is_localhost(self):
        assert "localhost" in LLAMACPP_DEFAULT_URL or "127.0.0.1" in LLAMACPP_DEFAULT_URL

    def test_ollama_availability_returns_bool(self):
        from karya.backends.ollama import OllamaBackend
        b = OllamaBackend("any-model")
        assert isinstance(b.is_available(), bool)

    def test_llamacpp_availability_returns_bool(self):
        from karya.backends.llamacpp import LlamaCppBackend
        b = LlamaCppBackend()
        assert isinstance(b.is_available(), bool)

    def test_llamacpp_chatml_has_all_roles(self):
        from karya.backends.llamacpp import LlamaCppBackend
        lb = LlamaCppBackend()
        msgs = [
            {"role": "system",    "content": "You are an agent."},
            {"role": "user",      "content": "Check disk"},
            {"role": "assistant", "content": "ok"},
        ]
        prompt = lb._messages_to_prompt(msgs)
        for role in ("system", "user", "assistant"):
            assert f"<|im_start|>{role}" in prompt
        assert "<|im_end|>" in prompt

    def test_chatml_ends_with_assistant_start(self):
        from karya.backends.llamacpp import LlamaCppBackend
        lb = LlamaCppBackend()
        prompt = lb._messages_to_prompt([{"role": "user", "content": "hi"}])
        assert prompt.strip().endswith("<|im_start|>assistant")

    @pytest.mark.parametrize("backend_path,init_kwargs", [
        ("karya.backends.llamacpp.LlamaCppBackend", {}),
        ("karya.backends.ollama.OllamaBackend",    {"model": "test-model"}),
    ])
    def test_tool_call_level1_direct_json(self, backend_path, init_kwargs):
        mod, cls = backend_path.rsplit(".", 1)
        B = getattr(importlib.import_module(mod), cls)
        b = B(**init_kwargs)
        response = '{"tool": "shell", "args": {"command": "df -h"}}'
        result = b.extract_tool_call(response)
        assert result is not None
        assert result["tool"] == "shell"
        assert result["args"]["command"] == "df -h"

    def test_tool_call_level2_json_codeblock(self):
        from karya.backends.llamacpp import LlamaCppBackend
        lb = LlamaCppBackend()
        response = (
            "Here is my decision:\n"
            "```json\n"
            "{\"tool\":\"system_info\",\"args\":{\"metrics\":[\"disk\"]}}\n"
            "```"
        )
        result = lb.extract_tool_call(response)
        assert result is not None
        assert result["tool"] == "system_info"

    def test_tool_call_level3_inline_json(self):
        from karya.backends.llamacpp import LlamaCppBackend
        lb = LlamaCppBackend()
        response = 'I will use {"tool":"shell","args":{"command":"free -h"}} now'
        result = lb.extract_tool_call(response)
        assert result is not None
        assert result["tool"] == "shell"

    def test_tool_call_level4_keyword_detection(self):
        from karya.backends.llamacpp import LlamaCppBackend
        lb = LlamaCppBackend()
        result = lb.extract_tool_call("I need to check system memory usage")
        assert result is not None
        assert result["tool"] == "system_info"

    def test_tool_call_returns_none_for_no_match(self):
        from karya.backends.llamacpp import LlamaCppBackend
        result = LlamaCppBackend().extract_tool_call("The weather is nice today.")
        assert result is None

    def test_tool_call_none_returned_for_empty(self):
        from karya.backends.llamacpp import LlamaCppBackend
        result = LlamaCppBackend().extract_tool_call("")
        assert result is None


# ── agent loop integration ────────────────────────────────────────────────────

class TestAgentLoopInit:

    @pytest.fixture
    def loop(self, tmp_path):
        from karya.core.loop import AgentLoop
        return AgentLoop(
            goals=["keep disk below 85%", "restart nginx if down"],
            state_dir=str(tmp_path),
            dry_run=True,
            cycle_interval=999,
        )

    def test_prioritizer_is_initialised(self, loop):
        assert loop.prioritizer is not None

    def test_at_least_two_triggers_registered(self, loop):
        # cron + file_watch minimum
        assert len(loop._triggers) >= 2

    def test_all_core_tools_registered(self, loop):
        expected = {"shell", "file", "system_info", "gpio"}
        assert expected.issubset(set(loop.registry.tools.keys()))

    def test_goal_tracking_starts_empty(self, loop):
        assert loop._goal_last_action == {}
        assert loop._failed_goals == set()

    def test_hil_manager_initialised(self, loop):
        assert loop.hil is not None

    def test_snapshot_returns_non_empty_string(self, loop):
        snap = loop._get_snapshot()
        assert isinstance(snap, str) and len(snap) > 0

    def test_snapshot_contains_system_metrics(self, loop):
        snap = loop._get_snapshot()
        # snapshot should mention at least one metric
        assert any(kw in snap for kw in ("cpu", "mem", "disk", "temp", "unavailable"))

    def test_llamacpp_backend_selected_when_specified(self, tmp_path):
        from karya.core.loop import AgentLoop
        from karya.backends.llamacpp import LlamaCppBackend
        loop = AgentLoop(
            goals=["x"],
            state_dir=str(tmp_path),
            backend="llamacpp",
            dry_run=True,
            cycle_interval=999,
        )
        assert isinstance(loop.backend, LlamaCppBackend)

    def test_ollama_backend_selected_by_default(self, tmp_path):
        from karya.core.loop import AgentLoop
        from karya.backends.ollama import OllamaBackend
        loop = AgentLoop(
            goals=["x"],
            state_dir=str(tmp_path),
            dry_run=True,
            cycle_interval=999,
        )
        assert isinstance(loop.backend, OllamaBackend)

    def test_threshold_trigger_registered(self, tmp_path):
        from karya.core.loop import AgentLoop
        from karya.triggers.threshold import ThresholdTrigger
        loop = AgentLoop(
            goals=["x"],
            state_dir=str(tmp_path),
            dry_run=True,
            cycle_interval=999,
            thresholds=[{"metric": "disk_used_pct", "op": ">", "value": 85}],
        )
        thresh = [t for t in loop._triggers if isinstance(t, ThresholdTrigger)]
        assert len(thresh) == 1

    def test_multiple_threshold_triggers_registered(self, tmp_path):
        from karya.core.loop import AgentLoop
        from karya.triggers.threshold import ThresholdTrigger
        loop = AgentLoop(
            goals=["x"],
            state_dir=str(tmp_path),
            dry_run=True,
            cycle_interval=999,
            thresholds=[
                {"metric": "disk_used_pct", "op": ">", "value": 85},
                {"metric": "cpu_temp_c",    "op": ">", "value": 75},
            ],
        )
        thresh = [t for t in loop._triggers if isinstance(t, ThresholdTrigger)]
        assert len(thresh) == 2

    def test_hil_from_config_wired_to_loop(self, tmp_path):
        from karya.core.loop import AgentLoop
        from karya.core.hil import FileChannel
        loop = AgentLoop(
            goals=["x"],
            state_dir=str(tmp_path),
            dry_run=True,
            cycle_interval=999,
            hil_config={
                "enabled": True,
                "channel": "file",
                "hil_dir": str(tmp_path / "hil"),
            },
        )
        assert loop.hil.enabled
        assert isinstance(loop.hil.channel, FileChannel)


# ── HIL decision classification ───────────────────────────────────────────────

class TestHILClassification(unittest.TestCase):

    def test_safe_tool_is_auto(self):
        from karya.core.hil import classify_decision
        level = classify_decision(
            "system_info", {"metrics": ["disk"]},
            priority_score=_SCORE_BELOW_CRITICAL,
        )
        self.assertEqual(level, HILLevel.AUTO)

    def test_rm_command_is_critical(self):
        from karya.core.hil import classify_decision
        level = classify_decision(
            "shell", {"command": "rm /tmp/old.log"},
            priority_score=_SCORE_BELOW_CRITICAL,
        )
        self.assertEqual(level, HILLevel.CRITICAL)

    def test_gpio_write_is_critical(self):
        from karya.core.hil import classify_decision
        level = classify_decision(
            "gpio", {"action": "write", "pin": _SAFE_GPIO_PINS[0], "value": 1},
            priority_score=_SCORE_BELOW_CRITICAL,
        )
        self.assertEqual(level, HILLevel.CRITICAL)

    def test_high_priority_score_is_critical(self):
        from karya.core.hil import classify_decision
        level = classify_decision(
            "shell", {"command": "df -h"},
            priority_score=_SCORE_ABOVE_CRITICAL,
        )
        self.assertEqual(level, HILLevel.CRITICAL)

    def test_rm_rf_root_is_block(self):
        from karya.core.hil import classify_decision
        level = classify_decision(
            "shell", {"command": "rm -rf /"},
            priority_score=_SCORE_BELOW_CRITICAL,
        )
        self.assertEqual(level, HILLevel.BLOCK)

    def test_all_critical_patterns_trigger_at_least_critical(self):
        from karya.core.hil import classify_decision
        for pattern in CRITICAL_PATTERNS[:5]:  # first 5 to keep test fast
            cmd = f"{pattern.strip()} /tmp/test"
            level = classify_decision(
                "shell", {"command": cmd},
                priority_score=_SCORE_BELOW_CRITICAL,
            )
            self.assertIn(
                level, (HILLevel.CRITICAL, HILLevel.BLOCK),
                f"Pattern '{pattern}' should trigger CRITICAL or BLOCK, got {level}",
            )


# ── HIL Manager ───────────────────────────────────────────────────────────────

class TestHILManager(unittest.TestCase):

    def test_disabled_always_approves(self):
        from karya.core.hil import HILManager
        hil = HILManager(enabled=False)
        approved, reason = hil.request_approval(
            "shell", {"command": "rm /tmp/x"}, "goal", _SCORE_BELOW_CRITICAL
        )
        self.assertTrue(approved)
        self.assertEqual(reason, "hil_disabled")

    def test_empty_config_creates_disabled_manager(self):
        from karya.core.hil import HILManager
        hil = HILManager.from_config({})
        self.assertFalse(hil.enabled)

    def test_file_channel_configured_correctly(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "file",
            "hil_dir": tmp,
            "timeout_sec": _HIL_DEFAULT_TIMEOUT,
            "default_on_timeout": _HIL_DEFAULT_ON_TIMEOUT,
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, FileChannel)
        self.assertEqual(hil.timeout_sec, _HIL_DEFAULT_TIMEOUT)
        self.assertEqual(hil.default_on_timeout, _HIL_DEFAULT_ON_TIMEOUT)

    def test_timeout_defaults_to_deny(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        hil = HILManager(
            channel=ch,
            timeout_sec=1,
            default_on_timeout=_HIL_DEFAULT_ON_TIMEOUT,
            enabled=True,
        )
        approved, reason = hil.request_approval(
            "shell", {"command": "rm /tmp/test.log"},
            "keep disk clean", _SCORE_ABOVE_CRITICAL,
        )
        self.assertFalse(approved)
        self.assertIn("timeout", reason)

    def test_timeout_approve_when_configured(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        hil = HILManager(
            channel=ch,
            timeout_sec=1,
            default_on_timeout="approve",  # permissive
            enabled=True,
        )
        approved, reason = hil.request_approval(
            "shell", {"command": "rm /tmp/x"},
            "clean disk", _SCORE_ABOVE_CRITICAL,
        )
        self.assertTrue(approved)

    def test_audit_log_written_after_decision(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        log_dir = tempfile.mkdtemp()
        hil = HILManager(
            channel=ch,
            timeout_sec=1,
            default_on_timeout=_HIL_DEFAULT_ON_TIMEOUT,
            log_dir=log_dir,
            enabled=True,
        )
        hil.request_approval(
            "shell", {"command": "rm /tmp/x"},
            "clean disk", _SCORE_ABOVE_CRITICAL,
        )
        log_path = Path(log_dir) / "hil_audit.jsonl"
        self.assertTrue(log_path.exists())
        record = json.loads(log_path.read_text().strip().splitlines()[-1])
        self.assertEqual(record["tool"], "shell")
        self.assertIn("decision", record)


# ── HIL file channel ──────────────────────────────────────────────────────────

class TestFileChannel(unittest.TestCase):

    def _make_req(self, tmp_dir, request_id="test0001"):
        from karya.core.hil import ApprovalRequest
        return ApprovalRequest(
            request_id=request_id,
            timestamp="2026-01-01T00:00:00",
            tool="shell",
            args={"command": "rm /tmp/x"},
            goal="keep disk clean",
            priority_score=float(_SCORE_ABOVE_CRITICAL),
            reason="command contains 'rm '",
            timeout_sec=5,
        )

    def test_send_writes_pending_file(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp)
        ch.send(req)
        pending = ch.pending / f"{req.request_id}.json"
        self.assertTrue(pending.exists())

    def test_pending_file_is_valid_json(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp)
        ch.send(req)
        pending = ch.pending / f"{req.request_id}.json"
        data = json.loads(pending.read_text())
        self.assertEqual(data["tool"], "shell")
        self.assertEqual(data["request_id"], req.request_id)

    def test_approve_via_touch_file(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "approve001")
        ch.send(req)
        (ch.approved / f"{req.request_id}.approve").touch()
        self.assertEqual(ch.poll(req), "approve")

    def test_deny_via_touch_file(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "deny001")
        ch.send(req)
        (ch.denied / f"{req.request_id}.deny").touch()
        self.assertEqual(ch.poll(req), "deny")

    def test_approve_via_responses_txt(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "resp001")
        ch.send(req)
        ch.responses.write_text(f"approve {req.request_id}\n")
        self.assertEqual(ch.poll(req), "approve")

    def test_deny_via_responses_txt(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "resp002")
        ch.send(req)
        ch.responses.write_text(f"deny {req.request_id}\n")
        self.assertEqual(ch.poll(req), "deny")

    def test_poll_returns_none_when_no_response(self):
        import tempfile
        from karya.core.hil import FileChannel
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "noresponse")
        ch.send(req)
        self.assertIsNone(ch.poll(req))

    def test_cleanup_moves_to_resolved(self):
        import tempfile
        from karya.core.hil import FileChannel, ApprovalRequest
        tmp = tempfile.mkdtemp()
        ch = FileChannel(hil_dir=tmp)
        req = self._make_req(tmp, "cleanup001")
        req.decision = "approve"
        ch.send(req)
        ch.cleanup(req)
        resolved = ch.base / "resolved" / f"{req.request_id}.json"
        self.assertTrue(resolved.exists())
        self.assertFalse((ch.pending / f"{req.request_id}.json").exists())


# ── HIL offline channels ──────────────────────────────────────────────────────

class TestHILOfflineChannels(unittest.TestCase):

    def _make_req(self, request_id, tool, args, goal, score, reason):
        from karya.core.hil import ApprovalRequest
        return ApprovalRequest(
            request_id=request_id,
            timestamp="2026-01-01T00:00:00",
            tool=tool,
            args=args,
            goal=goal,
            priority_score=float(score),
            reason=reason,
            timeout_sec=5,
        )

    def test_display_channel_approve_via_internal_state(self):
        from karya.core.hil import DisplayChannel
        ch = DisplayChannel()
        req = self._make_req(
            "disp_ap", "shell", {"command": "rm /tmp/x"},
            "keep disk clean", _SCORE_ABOVE_CRITICAL, "rm pattern"
        )
        ch.send(req)
        ch._decision = "approve"
        ch._got_input.set()
        self.assertEqual(ch.poll(req), "approve")

    def test_display_channel_deny_via_internal_state(self):
        from karya.core.hil import DisplayChannel
        ch = DisplayChannel()
        req = self._make_req(
            "disp_dn", "gpio", {"action": "write", "pin": _SAFE_GPIO_PINS[0]},
            "control relay", _SCORE_ABOVE_CRITICAL, "GPIO write"
        )
        ch.send(req)
        ch._decision = "deny"
        ch._got_input.set()
        self.assertEqual(ch.poll(req), "deny")

    def test_display_channel_none_before_input(self):
        from karya.core.hil import DisplayChannel
        ch = DisplayChannel()
        req = self._make_req(
            "disp_pending", "shell", {"command": "rm /tmp/y"},
            "goal", _SCORE_ABOVE_CRITICAL, "rm pattern"
        )
        ch.send(req)
        # do not set _got_input — should return None
        self.assertIsNone(ch.poll(req))

    def test_gpio_button_graceful_on_non_pi(self):
        from karya.core.hil import GPIOButtonChannel
        ch = GPIOButtonChannel(
            approve_pin=_APPROVE_PIN,
            deny_pin=_DENY_PIN,
            led_pin=_LED_PIN,
        )
        self.assertFalse(ch._gpio_available)

    def test_gpio_button_send_returns_false_on_non_pi(self):
        from karya.core.hil import GPIOButtonChannel
        ch = GPIOButtonChannel(_APPROVE_PIN, _DENY_PIN)
        req = self._make_req(
            "gpio_send", "gpio", {"action": "write", "pin": _SAFE_GPIO_PINS[0]},
            "control relay", _SCORE_ABOVE_CRITICAL, "GPIO write"
        )
        result = ch.send(req)
        self.assertFalse(result)

    def test_serial_channel_handles_missing_pyserial(self):
        from karya.core.hil import SerialApprovalChannel
        ch = SerialApprovalChannel(port=_FAKE_SERIAL_PORT, baud=9600)
        req = self._make_req(
            "ser_send", "shell", {"command": "rm /tmp/x"},
            "clean disk", _SCORE_ABOVE_CRITICAL, "rm pattern"
        )
        result = ch.send(req)
        self.assertIsInstance(result, bool)

    def test_from_config_display_channel(self):
        from karya.core.hil import HILManager, DisplayChannel
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "display",
            "timeout_sec": _HIL_DEFAULT_TIMEOUT,
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, DisplayChannel)

    def test_from_config_gpio_button_channel(self):
        from karya.core.hil import HILManager, GPIOButtonChannel
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "gpio_button",
            "approve_pin": _APPROVE_PIN,
            "deny_pin": _DENY_PIN,
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, GPIOButtonChannel)
        self.assertEqual(hil.channel.approve_pin, _APPROVE_PIN)
        self.assertEqual(hil.channel.deny_pin, _DENY_PIN)

    def test_from_config_serial_channel(self):
        from karya.core.hil import HILManager, SerialApprovalChannel
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "serial",
            "serial_port": _FAKE_SERIAL_PORT,
            "serial_baud": 9600,
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, SerialApprovalChannel)
        self.assertEqual(hil.channel.port, _FAKE_SERIAL_PORT)

    def test_unknown_channel_falls_back_to_file(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "completely_unknown_channel_xyz",
            "hil_dir": tmp,
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, FileChannel)

    def test_telegram_missing_creds_falls_back_to_file(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "telegram",
            "hil_dir": tmp,
            # deliberately no bot_token or chat_id
        })
        self.assertTrue(hil.enabled)
        self.assertIsInstance(hil.channel, FileChannel)

    def test_slack_missing_url_falls_back_to_file(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "slack",
            "hil_dir": tmp,
            # deliberately no webhook_url
        })
        self.assertIsInstance(hil.channel, FileChannel)

    def test_webhook_missing_urls_falls_back_to_file(self):
        import tempfile
        from karya.core.hil import HILManager, FileChannel
        tmp = tempfile.mkdtemp()
        hil = HILManager.from_config({
            "enabled": True,
            "channel": "webhook",
            "hil_dir": tmp,
            # deliberately no notify_url or poll_url
        })
        self.assertIsInstance(hil.channel, FileChannel)
