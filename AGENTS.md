# AGENTS.md: promptli-hermes-plugin

Hermes Agent (Nous Research) platform plugin for Promptli Comms. The repo root *is* the plugin directory
(`plugin.yaml` + `__init__.py`), which is what `hermes plugins install <owner/repo>` and the plugin catalog
expect. Keep it thin: framing, state and call drivers belong in `promptli-gateway`.

- `__init__.py`: `register(ctx)`: platform, `pre_tool_call` hook, skill, `hermes promptli` CLI. Import-light
  (user plugins load in every `hermes` process); the SDK and adapter load lazily.
- `adapter.py`: `PromptliAdapter(BasePlatformAdapter)`: Agent Link consumer, sender checks, routes, send.
- `bridge.py`: `HermesTurnBridge`, a promptli-gateway `RuntimeAdapter` that runs call turns as Hermes events.
- `guard.py`: TRAI calling hours, AI disclosure, no outbound tools in strangers' sessions.
- `settings.py`, `store.py`, `prompts.py`, `cli.py`, `skills/promptli-comms/SKILL.md`.

Rules:
- Only the public plugin API (see Hermes's "Public plugin API" page). No monkeypatching of Hermes core.
- Secrets only through `get_scoped_secret` (read) and `hermes_cli.config.save_env_value` (write). Never
  `os.environ`, never argv, never print or log a key. Never store an organization key.
- `provides_tools` / `provides_hooks` in `plugin.yaml` must match what `register()` registers.
- Dependencies keep upper bounds (`promptli>=0.2.2,<0.3`).
- Checks: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`, plus
  `hermes plugins validate . --install-deps` before any catalog pin bump.
