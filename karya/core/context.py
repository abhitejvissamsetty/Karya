"""
context.py — context window budget manager
Pure rule-based. Zero LLM calls. Fits history into token budget
by dropping oldest turns and truncating tool results first.
"""

import re
from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token. Fast, no model needed."""
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(str(block))
        total += 4  # role + overhead per message
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Hard truncate a string to fit within token budget."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " …[truncated]"


class ContextManager:
    """
    Manages the rolling message history to fit within hardware token budget.

    Strategy (in order of preference):
    1. Truncate tool results (biggest, lowest value)
    2. Drop oldest turn pairs (user + assistant together)
    3. Shorten system prompt to minimal version
    4. Hard truncate current user message
    """

    def __init__(self, max_tokens: int, system_tokens: int,
                 tool_result_tokens: int, history_tokens: int):
        self.max_tokens = max_tokens
        self.system_token_budget = system_tokens
        self.tool_result_budget = tool_result_tokens
        self.history_token_budget = history_tokens

    def build_messages(
        self,
        system_prompt: str,
        history: list[dict],
        current_user_msg: str,
    ) -> list[dict]:
        """
        Build the final messages list that fits within budget.
        Returns messages ready to send to the model.
        """
        # 1. Truncate system prompt if needed
        system = truncate_to_tokens(system_prompt, self.system_token_budget)

        # 2. Truncate tool results in history
        trimmed_history = self._truncate_tool_results(history)

        # 3. Drop oldest pairs until we fit
        trimmed_history = self._drop_oldest_pairs(
            trimmed_history, current_user_msg, system
        )

        # 4. Build final list
        messages = [{"role": "system", "content": system}]
        messages.extend(trimmed_history)
        messages.append({"role": "user", "content": current_user_msg})
        return messages

    def _truncate_tool_results(self, history: list[dict]) -> list[dict]:
        """Truncate any tool result content that exceeds budget."""
        result = []
        for msg in history:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if estimate_tokens(content) > self.tool_result_budget:
                    msg = dict(msg)
                    msg["content"] = truncate_to_tokens(
                        content, self.tool_result_budget
                    )
            result.append(msg)
        return result

    def _drop_oldest_pairs(
        self,
        history: list[dict],
        current_msg: str,
        system: str,
    ) -> list[dict]:
        """Drop oldest user+assistant pairs until total fits in budget."""
        while True:
            total = (
                estimate_tokens(system)
                + estimate_messages_tokens(history)
                + estimate_tokens(current_msg)
                + 50  # headroom
            )
            if total <= self.max_tokens:
                break
            if len(history) < 2:
                break
            # drop first pair
            history = history[2:]
        return history

    def fits(self, messages: list[dict]) -> bool:
        return estimate_messages_tokens(messages) <= self.max_tokens

    def usage(self, messages: list[dict]) -> str:
        used = estimate_messages_tokens(messages)
        pct = int(used / self.max_tokens * 100)
        return f"{used}/{self.max_tokens} tokens ({pct}%)"
