# Agent guidance for hermes-plugin-tick

Read this before you change code here. Humans: see `CONTRIBUTING.md`.

## What this repo is

A standalone Hermes plugin in three halves that share one state store:

| Half | Files |
| --- | --- |
| Agent | `__init__.py`, `plugin.yaml` |
| Dashboard backend | `dashboard/plugin_api.py`, `dashboard/manifest.json` |
| Desktop companion | `desktop/plugin.js` |

It extends Hermes only through public surfaces (hooks, `ctx.register_*`,
`ctx.state`, `ctx.get_config`). It never patches, wraps or rebinds Hermes
core. `docs/design.md` records each decision and invariant.

## Rules

- Work on a branch; never commit to `main`.
- Every behaviour ships with a test that fails without it. Show the test
  failing on the old code in the pull request.
- Every user-facing setting is declared in `plugin.yaml` `config_schema` and
  documented in `README.md`. A test enforces this.
- Hooks fail open: catch everything, log at debug, return `None`.
- Injected context is appended only to the current user message. Never touch
  the system prompt or past history (prompt-cache safety).
- `provides_hooks` and `provides_tools` match what `register()` registers.
- Keep `plugin.yaml` `name`, `dashboard/manifest.json` `name` and the desktop
  export `id` identical.
- A new feature extends the `enabled: false` master-switch test.

## Verify

```bash
python3 -m pytest -q tests/
node --test 'tests/js/*.test.mjs'
hermes plugins validate .
```

CI fails if any test is skipped, and on any `hermes plugins validate`
warning (it runs in the Hermes image pinned in the workflows).
