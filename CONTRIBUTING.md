# Contributing to karya

Thank you for taking the time to contribute.

## Getting started

```bash
git clone https://github.com/yourusername/karya
cd karya
pip install -e ".[all]"
python -m pytest          # all 62 tests should pass
```

## Branch naming

| Prefix | Use for |
|--------|---------|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation only |
| `test/` | Test additions or fixes |
| `refactor/` | Code restructuring with no behaviour change |

## Pull requests

- Open an issue first for any non-trivial change
- Keep PRs focused — one thing per PR
- All tests must pass: `python -m pytest`
- Add tests for new behaviour
- Update `CHANGELOG.md` under `[Unreleased]`

## Adding a new tool

1. Create `karya/tools/yourtool.py` with a class that has `name`, `description`, `schema`, and `run()` 
2. Register it in `karya/tools/__init__.py` or in `loop.py`
3. Add safety checks to `karya/core/safety.py` if the tool can cause harm
4. Write tests in `tests/test_karya.py`

## Adding a new trigger

1. Create `karya/triggers/yourtrigger.py` inheriting from `BaseTrigger`
2. Implement `_run()` — it should block until `_stop_event` is set
3. Call `self.fire(reason, data)` when the trigger fires
4. Degrade gracefully if required libraries are not installed
5. Wire it into `AgentLoop._setup_triggers()` in `loop.py`
6. Write tests

## Code style

- No external formatter required — keep it readable
- Type hints on all public functions
- Docstrings on all public classes and methods
- No dependencies added to core — stdlib only

## Reporting bugs

Open a GitHub issue with:
- Your hardware (Pi model, RAM)
- Python version
- Ollama/llama.cpp version and model
- The full error output
- Your `goals.yaml` (redact sensitive paths if needed)
