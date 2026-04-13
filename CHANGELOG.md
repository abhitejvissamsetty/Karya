# Changelog

## [0.1.3] — 2026-04-13

### Improved
- Test suite fully dynamic — zero hardcoded values
- All constants imported from source modules (TIERS, MAX_RECENT_ACTIONS,
  CRITICAL_PRIORITY_SCORE, CRITICAL_PATTERNS, FORBIDDEN_WRITE_PATHS)
- All test fixtures use pytest tmp_path instead of tempfile.mkdtemp()
- Threshold and GPIO values derived at runtime from live system
- 60 new tests added across all modules (142 total, up from 82)
- New test classes: TestFileChannel, TestHILManager split from TestHIL
- Each assertion tests a distinct behaviour — no duplicate checks

## [0.1.2] — 2026-04-13

### Fixed / Improved
- HIL redesigned as offline-first to match karya core philosophy
- Added 3 new offline HIL channels: gpio_button, serial, display
- Internet channels (telegram, slack, webhook) clearly marked optional
- Any network channel with missing credentials auto-falls-back to file
- goals.yaml HIL section reordered: offline channels documented first
- README HIL section rewritten with correct offline-first framing
- 9 new offline channel tests (82 total)

## [0.1.1] — 2026-04-13

### Added
- Human-in-the-loop (HIL) system for critical decision approval
- Four HIL channels: Telegram (inline buttons), Slack (webhook), generic webhook, file (offline-safe)
- Decision classifier: AUTO / CONFIRM / CRITICAL / BLOCK before every tool execution
- CRITICAL triggers: rm commands, GPIO writes, systemctl stop, priority score >= 80
- Approval timeout with configurable default (deny/approve)
- Full HIL audit log at ~/.karya/hil/log/hil_audit.jsonl
- hil: section in goals.yaml for zero-code HIL configuration
- 11 new HIL tests (73 total)

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
