"""
backends/llamacpp.py — direct llama.cpp backend
Talks directly to llama-server (llama.cpp HTTP server).
No Ollama overhead — better for Pi Zero / very low RAM devices.

llama-server launch example:
  llama-server \
    -m ~/models/qwen2.5-1.5b-q4_k_m.gguf \
    -c 2048 \
    --host 0.0.0.0 \
    --port 8080 \
    -ngl 0 \
    --threads 4

Or with quantized KV cache (saves RAM on Pi):
  llama-server -m model.gguf -c 2048 \
    --cache-type-k q4_0 --cache-type-v q4_0
"""

import json
import logging
import re
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("karya.llamacpp")

DEFAULT_BASE_URL = "http://localhost:8080"


class LlamaCppBackend:
    """
    Talks to llama-server via its OpenAI-compatible /v1/chat/completions endpoint.
    Falls back to the native /completion endpoint if chat completions unavailable.
    """

    def __init__(
        self,
        model: str = "",                    # model name (informational only for llama.cpp)
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 600,                 # 10 min — Pi Zero is very slow
        temperature: float = 0.1,
        max_tokens: int = 256,              # keep short — saves RAM
        n_threads: Optional[int] = None,    # None = let llama.cpp decide
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.n_threads = n_threads
        self._use_chat_api = None   # detected on first call

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return data.get("status") in ("ok", "no slot available", "loading model")
        except Exception:
            # also try /v1/models as fallback
            try:
                req = urllib.request.Request(f"{self.base_url}/v1/models")
                with urllib.request.urlopen(req, timeout=5):
                    return True
            except Exception:
                return False

    def get_model_info(self) -> dict:
        """Return model properties from llama-server."""
        try:
            req = urllib.request.Request(f"{self.base_url}/props")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}

    def chat(
        self,
        messages: list,
        stream: bool = True,
        tools: Optional[list] = None,
    ) -> str:
        """
        Send messages. Tries OpenAI-compatible chat endpoint first,
        falls back to native /completion endpoint.
        """
        if self._use_chat_api is None:
            self._use_chat_api = self._detect_chat_api()

        if self._use_chat_api:
            return self._chat_completions(messages, stream, tools)
        else:
            return self._native_completion(messages, stream)

    # ── OpenAI-compatible endpoint (/v1/chat/completions) ─────────────────────

    def _chat_completions(self, messages: list, stream: bool, tools) -> str:
        payload = {
            "model": self.model or "local",
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if self.n_threads:
            payload["n_threads"] = self.n_threads

        url = f"{self.base_url}/v1/chat/completions"
        return self._post(url, payload, stream, chat_format=True)

    # ── Native llama.cpp endpoint (/completion) ────────────────────────────────

    def _native_completion(self, messages: list, stream: bool) -> str:
        """Convert messages to a single prompt string for /completion endpoint."""
        prompt = self._messages_to_prompt(messages)
        payload = {
            "prompt": prompt,
            "stream": stream,
            "temperature": self.temperature,
            "n_predict": self.max_tokens,
            "stop": ["</s>", "<|im_end|>", "\nTrigger:", "\nSYSTEM:"],
        }
        if self.n_threads:
            payload["n_threads"] = self.n_threads

        url = f"{self.base_url}/completion"
        return self._post(url, payload, stream, chat_format=False)

    def _post(self, url: str, payload: dict, stream: bool, chat_format: bool) -> str:
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
                    print("\n\033[36m[agent]\033[0m ", end="", flush=True)
                    for line in resp:
                        line = line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        # SSE format: "data: {...}"
                        if line.startswith("data: "):
                            line = line[6:]
                        if line == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        token = self._extract_token(chunk, chat_format)
                        if token:
                            print(token, end="", flush=True)
                            full_response += token

                        if self._is_done(chunk, chat_format):
                            break
                    print()
                else:
                    data = json.loads(resp.read())
                    full_response = self._extract_full(data, chat_format)

        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot reach llama-server at {self.base_url}: {e}")

        return full_response

    # ── helpers ───────────────────────────────────────────────────────────────

    def _detect_chat_api(self) -> bool:
        """Check if /v1/chat/completions is available."""
        try:
            req = urllib.request.Request(f"{self.base_url}/v1/models")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False

    def _extract_token(self, chunk: dict, chat_format: bool) -> str:
        if chat_format:
            # OpenAI SSE format
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                return delta.get("content", "")
        else:
            # native llama.cpp format
            return chunk.get("content", "")
        return ""

    def _is_done(self, chunk: dict, chat_format: bool) -> bool:
        if chat_format:
            choices = chunk.get("choices", [])
            return bool(choices and choices[0].get("finish_reason"))
        return chunk.get("stop", False)

    def _extract_full(self, data: dict, chat_format: bool) -> str:
        if chat_format:
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        return data.get("content", "")

    def _messages_to_prompt(self, messages: list) -> str:
        """
        Convert OpenAI-style messages to ChatML format for native endpoint.
        ChatML is what Qwen, Hermes, and most modern small models use.
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append("<|im_start|>assistant\n")
        return "\n".join(parts)

    def extract_tool_call(self, response: str) -> Optional[dict]:
        """Same 4-level fallback as Ollama backend."""
        response = response.strip()

        # Level 1: direct JSON
        try:
            data = json.loads(response)
            if "tool" in data:
                return data
        except json.JSONDecodeError:
            pass

        # Level 2: ```json block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if "tool" in data:
                    return {"tool": data["tool"],
                            "args": data.get("args", data.get("arguments", {}))}
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

        # Level 4: keyword detection
        patterns = {
            "shell": r"(?:run|execute|shell)[:\s]+`([^`]+)`",
            "system_info": r"check\s+(?:system|memory|disk|cpu)",
        }
        for tool_name, pattern in patterns.items():
            m = re.search(pattern, response, re.IGNORECASE)
            if m:
                args = {"command": m.group(1)} if tool_name == "shell" else {}
                return {"tool": tool_name, "args": args}

        return None
