# Changelog

## [0.1.0] — 2026-04-13

### Added
- Hardware tier auto-detection (`nano` / `micro` / `small` / `base`) based on RAM
- Rule-based context window manager — sliding window, no LLM needed
- Compact world state (JSON on disk, survives reboots, ≤300 tokens when serialized)
- Safety guard rails — forbidden patterns, confirm delays, protected paths, GPIO whitelists
- **Multi-goal priority ranker** — urgency keywords, metric proximity, trigger source,
  failure history, staleness — highest urgency floats to top automatically
- Ollama backend with streaming + 4-level tool call fallback parser
- llama.cpp direct backend (lower overhead, Pi Zero friendly)
- Tools: `shell`, `file`, `system_info`, `gpio`, `serial`
- Triggers: `cron`, `file_watch`, `threshold`, `gpio`, `serial`
- CLI: `start`, `run-once`, `doctor`, `status`, `bench`
- Systemd service file for boot-time autostart
- 62-test suite covering all components
- GitHub Actions CI (Python 3.10 / 3.11 / 3.12)
