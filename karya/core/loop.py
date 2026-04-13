"""
loop.py — main perception → decision → action loop
Integrates all triggers. Runs forever, offline, no user input needed.
Each trigger fires into a shared queue — the agent processes them in order.
"""

import json
import logging
import queue
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from karya.core.context import ContextManager
from karya.core.hil import HILManager, HILLevel
from karya.core.hw_detect import HardwareTier, detect_tier
from karya.core.priority import GoalPrioritizer, build_priority_prompt
from karya.core.safety import SafetyGuard
from karya.core.state import StateManager
from karya.backends.ollama import OllamaBackend
from karya.backends.llamacpp import LlamaCppBackend
from karya.tools import ShellTool, FileTool, SystemInfoTool, ToolRegistry
from karya.tools.gpio import GPIOTool
from karya.tools.serial_tool import SerialTool
from karya.triggers.base import TriggerEvent
from karya.triggers.cron import CronTrigger
from karya.triggers.file_watch import FileWatchTrigger
from karya.triggers.threshold import ThresholdTrigger
from karya.triggers.gpio import GPIOTrigger

logger = logging.getLogger("karya.loop")

SYSTEM_PROMPT_TEMPLATE = """\
You are an autonomous edge agent on low-power hardware. No internet. Fully offline.
Achieve the goals below using the available tools. Be decisive and concise.

{state_block}

TIME: {time}
SYSTEM: {snapshot}
TOOLS: {tools}

RULES:
- Respond with EXACTLY one JSON tool call: {{"tool":"<n>","args":{{...}}}}
- If nothing to do: {{"tool":"none","args":{{}}}}
- No explanation. No extra text. JSON only.\
"""


class AgentLoop:
    def __init__(
        self,
        goals: list,
        model: Optional[str] = None,
        base_url: str = "http://localhost:11434",
        backend: str = "ollama",
        dry_run: bool = False,
        safe_gpio_pins: Optional[list] = None,
        state_dir=None,
        cycle_interval: Optional[int] = None,
        watch_dir: Optional[str] = None,
        gpio_triggers: Optional[list] = None,
        serial_triggers: Optional[list] = None,
        thresholds: Optional[list] = None,
        serial_tool_port: Optional[str] = None,
        hil_config: Optional[dict] = None,      # human-in-the-loop config
    ):
        self.goals = goals
        self._event_queue: queue.Queue = queue.Queue()
        self._triggers = []

        tier: HardwareTier = detect_tier()
        logger.info("Tier: %s | model: %s | ctx: %d tokens",
                    tier.name, tier.recommended_model, tier.max_ctx_tokens)
        self.tier = tier
        self.cycle_interval = cycle_interval or tier.cycle_interval_sec
        self.model = model or tier.recommended_model

        self.state = StateManager(Path(state_dir) if state_dir else None)
        self.state.set_goals(goals)

        self.context = ContextManager(
            max_tokens=tier.max_ctx_tokens,
            system_tokens=tier.system_tokens,
            tool_result_tokens=tier.tool_result_tokens,
            history_tokens=tier.history_tokens,
        )
        self.safety = SafetyGuard(
            safe_gpio_pins=safe_gpio_pins or [],
            dry_run=dry_run,
        )

        # HIL manager — human approval for critical decisions
        self.hil = HILManager.from_config(hil_config or {})
        if self.hil.enabled:
            logger.info("HIL enabled — channel: %s | timeout: %ds",
                        type(self.hil.channel).__name__, self.hil.timeout_sec)

        # backend selection
        if backend == "llamacpp":
            llamacpp_url = base_url if "808" in base_url else "http://localhost:8080"
            self.backend = LlamaCppBackend(model=self.model, base_url=llamacpp_url)
            logger.info("Using llama.cpp backend at %s", llamacpp_url)
        else:
            self.backend = OllamaBackend(model=self.model, base_url=base_url)
            logger.info("Using Ollama backend at %s", base_url)

        self.prioritizer = GoalPrioritizer()
        self._goal_last_action: dict = {}
        self._failed_goals: set = set()

        self.registry = ToolRegistry(
            safety_guard=self.safety,
            tool_result_max_chars=tier.tool_result_tokens * 4,
        )
        self.registry.register(ShellTool())
        self.registry.register(FileTool())
        self.registry.register(SystemInfoTool())
        self.registry.register(GPIOTool())
        if serial_tool_port:
            self.registry.register(SerialTool())

        self._setup_triggers(
            watch_dir=watch_dir,
            gpio_triggers=gpio_triggers or [],
            serial_triggers=serial_triggers or [],
            thresholds=thresholds or [],
        )
        self._history: list = []

    def _setup_triggers(self, watch_dir, gpio_triggers, serial_triggers, thresholds):
        def _enqueue(event: TriggerEvent):
            self._event_queue.put(event)

        self._triggers.append(
            CronTrigger(interval_seconds=self.cycle_interval,
                        callback=_enqueue, fire_immediately=True)
        )
        self._triggers.append(
            FileWatchTrigger(watch_dir=watch_dir, callback=_enqueue)
        )
        for cfg in gpio_triggers:
            self._triggers.append(
                GPIOTrigger(pin=cfg["pin"], edge=cfg.get("edge", "falling"),
                            pull_up=cfg.get("pull_up", True), callback=_enqueue)
            )
        for cfg in serial_triggers:
            from karya.triggers.serial import SerialTrigger as ST
            self._triggers.append(
                ST(port=cfg["port"], baud=cfg.get("baud", 9600),
                   trigger_on=cfg.get("trigger_on", "any"),
                   keywords=cfg.get("keywords", []), callback=_enqueue)
            )
        for cfg in thresholds:
            self._triggers.append(
                ThresholdTrigger(metric=cfg["metric"], operator=cfg["op"],
                                 threshold=cfg["value"],
                                 check_every=cfg.get("check_every", 30),
                                 hysteresis=cfg.get("hysteresis", 2),
                                 callback=_enqueue)
            )

    def _start_triggers(self):
        for t in self._triggers:
            t.start()
        logger.info("Started %d triggers", len(self._triggers))

    def _stop_triggers(self):
        for t in self._triggers:
            t.stop()

    def _get_snapshot(self) -> str:
        try:
            data = json.loads(SystemInfoTool().run(["all"]))
            parts = []
            if data.get("cpu", {}).get("usage_pct") is not None:
                parts.append(f"cpu={data['cpu']['usage_pct']}%")
            if "memory" in data:
                m = data["memory"]
                parts.append(f"mem={m.get('used_pct','?')}%({m.get('free_mb','?')}MB free)")
            if "disk" in data:
                d = data["disk"]
                parts.append(f"disk={d.get('used_pct','?')}%({d.get('free_gb','?')}GB free)")
            if data.get("temp", {}).get("cpu_temp_c"):
                parts.append(f"temp={data['temp']['cpu_temp_c']}°C")
            return " | ".join(parts) or "unavailable"
        except Exception:
            return "unavailable"

    def run_once(self, trigger: str = "cron", extra_context: str = "") -> dict:
        cycle = self.state.get().cycle_count + 1
        print(f"\n{'─'*52}")
        print(f"  cycle #{cycle} | {datetime.now().strftime('%H:%M:%S')} | {trigger}")
        print(f"{'─'*52}")

        # rank goals — most urgent first
        facts = self.state.get().facts
        ranked = self.prioritizer.rank(
            goals=self.goals,
            current_facts=facts,
            trigger_source=trigger,
            failed_goals=list(self._failed_goals),
            last_action_times=self._goal_last_action,
        )
        top = ranked[0] if ranked else None
        if top:
            urgency = "URGENT" if top.score >= 60 else "HIGH" if top.score >= 30 else "normal"
            print(f"  priority: [{urgency}] {top.goal[:55]} (score={top.score})")

        priority_block = build_priority_prompt(ranked, facts)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            state_block=priority_block + "\n" + self.state.to_prompt_block(),
            time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            snapshot=self._get_snapshot(),
            tools=", ".join(self.registry.tools.keys()),
        )
        user_msg = f"Trigger: {trigger}."
        if extra_context:
            user_msg += f" Context: {extra_context}"
        user_msg += " Focus on the highest priority goal. Decide next action."

        messages = self.context.build_messages(
            system_prompt=system_prompt,
            history=self._history,
            current_user_msg=user_msg,
        )
        print(f"  context : {self.context.usage(messages)}")

        try:
            response = self.backend.chat(messages, stream=True)
        except ConnectionError as e:
            logger.error("Backend unavailable: %s", e)
            return {"error": str(e), "success": False}

        decision = self._parse_decision(response) or {"tool": "none", "args": {}}
        tool_name = decision.get("tool", "none")
        args = decision.get("args", {})
        result, success = self._execute_decision(
            decision,
            goal=top.goal if top else "",
            score=top.score if top else 0,
        )
        print(f"  result  : {result[:100]}")

        self.state.record_action(trigger=trigger, tool=tool_name,
                                 args=args, result=result, success=success)

        # update priority tracking
        if top:
            self._goal_last_action[top.goal] = time.time()
            if not success and tool_name != "none":
                self._failed_goals.add(top.goal)
            else:
                self._failed_goals.discard(top.goal)

        self._history.append({"role": "user", "content": user_msg})
        self._history.append({"role": "assistant", "content": response})

        return {"trigger": trigger, "tool": tool_name,
                "args": args, "result": result, "success": success,
                "priority_score": top.score if top else 0,
                "active_goal": top.goal if top else ""}

    def _parse_decision(self, response: str) -> Optional[dict]:
        response = response.strip()
        try:
            data = json.loads(response)
            if "tool" in data:
                return data
        except json.JSONDecodeError:
            pass
        return self.backend.extract_tool_call(response)

    def _execute_decision(self, decision: dict, goal: str = "", score: float = 0) -> tuple:
        tool_name = decision.get("tool", "none")
        args = decision.get("args", {})
        if tool_name == "none":
            return "no action needed", True

        # HIL gate — check if human approval required before executing
        if self.hil.enabled:
            level, reason = self.hil.needs_approval(tool_name, args, score)
            if level.value == "critical":
                approved, hil_reason = self.hil.request_approval(
                    tool=tool_name,
                    args=args,
                    goal=goal,
                    priority_score=score,
                )
                if not approved:
                    return f"[HIL denied: {hil_reason}]", False
            elif level.value == "block":
                return "[HIL blocked: matches forbidden pattern]", False

        result, success = self.registry.execute(tool_name, args)
        if tool_name == "system_info":
            try:
                data = json.loads(result)
                for section, key, fact in [
                    ("disk",   "used_pct",   "disk_used_pct"),
                    ("memory", "used_pct",   "mem_used_pct"),
                    ("temp",   "cpu_temp_c", "cpu_temp_c"),
                ]:
                    val = data.get(section, {}).get(key)
                    if val is not None:
                        self.state.update_fact(fact, val)
            except Exception:
                pass
        return result, success

    def run_forever(self):
        self._print_banner()
        if not self.backend.is_available():
            print(f"\n  [warn] Ollama not reachable at {self.backend.base_url}")
            print(f"  Start : ollama serve")
            print(f"  Pull  : ollama pull {self.model}\n")

        self._start_triggers()
        try:
            while True:
                try:
                    event: TriggerEvent = self._event_queue.get(timeout=1.0)
                    extra = event.data.get("content") or event.data.get("message") or ""
                    self.run_once(trigger=event.reason, extra_context=extra[:200])
                except queue.Empty:
                    continue
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.exception("Cycle error: %s", e)
                    time.sleep(2)
        except KeyboardInterrupt:
            print("\n  stopping...")
        finally:
            self._stop_triggers()
            print("  karya stopped.")

    def _print_banner(self):
        print(f"\n{'═'*52}")
        print("  karya — offline autonomous agent")
        print(f"{'═'*52}")
        print(f"  model    : {self.model}")
        print(f"  tier     : {self.tier.name}")
        print(f"  ctx      : {self.tier.max_ctx_tokens} tokens")
        print(f"  interval : {self.cycle_interval}s (cron heartbeat)")
        print(f"  triggers : {len(self._triggers)}")
        print(f"  goals    :")
        for g in self.goals:
            print(f"    • {g}")
        print(f"{'═'*52}")
