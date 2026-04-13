"""
backends/ollama.py — Ollama local API backend with streaming
Handles context length config, streaming output, tool call parsing.
"""

import json
import logging
import re
import time
import urllib.request
import urllib.error
from typing import Optional, Generator

logger = logging.getLogger("karya.ollama")

DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaBackend:
    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 300,     # 5 min — Pi inference is slow
        temperature: float = 0.1,  # low temp for deterministic decisions
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def chat(
        self,
        messages: list[dict],
        stream: bool = True,
        tools: Optional[list[dict]] = None,
    ) -> str:
        """
        Send messages to Ollama, return full response string.
        Streams to stdout token-by-token if stream=True.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": self.temperature,
                "num_predict": 512,     # keep responses short on Pi
            },
        }
        if tools:
            payload["tools"] = tools

        url = f"{self.base_url}/api/chat"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )

        full_response = ""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if stream:
                    print("\n\033[32m[agent]\033[0m ", end="", flush=True)
                    for line in resp:
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line.decode())
                        except json.JSONDecodeError:
                            continue
                        msg = chunk.get("message", {})
                        token = msg.get("content", "")
                        if token:
                            print(token, end="", flush=True)
                            full_response += token
                        if chunk.get("done"):
                            print()  # newline at end
                            break
                else:
                    data = json.loads(resp.read())
                    full_response = data.get("message", {}).get("content", "")
        except urllib.error.URLError as e:
            logger.error("Ollama request failed: %s", e)
            raise ConnectionError(f"Cannot reach Ollama at {self.base_url}: {e}")

        return full_response

    def extract_tool_call(self, response: str) -> Optional[dict]:
        """
        Multi-level tool call extraction.
        Level 1: native Ollama tool_calls field (handled upstream)
        Level 2: ```json code block
        Level 3: bare JSON object in response
        Level 4: keyword detection
        Returns {"tool": str, "args": dict} or None
        """
        # Level 2: ```json block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if "tool" in data:
                    return {"tool": data["tool"], "args": data.get("args", data.get("arguments", {}))}
            except json.JSONDecodeError:
                pass

        # Level 3: bare JSON object
        match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\"tool\"(?:[^{}]|\{[^{}]*\})*\}", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if "tool" in data:
                    return {"tool": data["tool"], "args": data.get("args", {})}
            except json.JSONDecodeError:
                pass

        # Level 4: keyword detection (last resort)
        patterns = {
            "shell": r"(?:run|execute|shell)[:\s]+`([^`]+)`",
            "read_file": r"read[:\s]+([/~][^\s]+)",
            "system_info": r"check\s+(?:system|memory|disk|cpu)",
        }
        for tool_name, pattern in patterns.items():
            m = re.search(pattern, response, re.IGNORECASE)
            if m:
                return {"tool": tool_name, "args": {"command": m.group(1)} if tool_name == "shell" else {}}

        return None
