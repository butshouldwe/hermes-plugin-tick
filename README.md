# tick

A Hermes plugin that keeps agents grounded in time.

Agents do not know that time passed between two messages. They act on a CI
result from three hours ago, or forget a check they meant to run. tick fixes
that with one line per turn and a small working ledger.

```text
[tick] now Fri 2026-10-09 22:14 EDT (UTC-04:00) = 2026-10-10T02:14Z | last message 3h12m ago (19:02)
[tick] reconcile: stale gap (3h12m). Earlier observations of mutable state may be stale. Re-check them before acting on them.
Observed before this point (newest first):
- 3h12m ago: terminal tea pr view 19
- 3h20m ago: terminal make deploy -> error
Ledger (open):
- #4 open: review verdict on PR 19 (3h15m ago)
[tick] Due checks:
- #5 PR 19 CI (due 2h50m ago)
```

## What it does

| Part | When | Source |
| --- | --- | --- |
| Clock line | Every turn | Local time, zone name, UTC offset, UTC time, gap since the previous message |
| Training cutoff | Every turn, if configured | `cutoffs` setting |
| Observations | Recorded on matching tool calls | `post_tool_call` hook, no model involved |
| Ledger items | When the agent calls the `tick` tool | Decisions, open items, checks with a due time |
| Reconcile block | After a stale gap or a compaction | Observations, open items |
| Due checks | Every turn once a check is due | `tick` tool `after_minutes` |

Design rules:

- Context goes only into the current user message (`pre_llm_call`). The system
  prompt and past history do not change, so the prompt cache stays valid.
- State is per session in the plugin's profile-scoped `ctx.state`. The gap is
  correct after a restart.
- When compaction gives the session a new id, the ledger carries forward from
  the parent session.
- Every hook fails open. A tick error never blocks a turn.

## Install

```bash
hermes plugins install butshouldwe/hermes-plugin-tick --enable
```

## Configure

Settings live under `plugins.entries.tick.settings` in the profile's
`config.yaml`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch |
| `label` | `tick` | Tag on injected lines, shown as `[tick]` |
| `timezone` | `""` | IANA zone, for example `America/New_York`. Empty uses the Hermes `timezone` setting, then the host zone. An unknown zone falls back to UTC |
| `clock` | `true` | Add the time line on every turn |
| `stale_after_minutes` | `60` | Gap after which earlier observations count as stale |
| `recheck_hint` | generic | What the agent should re-check after a stale gap |
| `cutoffs` | `{}` | Model-name substring to cutoff text. Longest match wins |
| `ledger` | `true` | Record observations and register the `tick` tool |
| `observe` | `terminal`, `web_*`, `browser_*`, `mcp_*` | Tool-name globs recorded as observations |
| `ignore` | `[]` | Tool-name globs never recorded |
| `max_observations` | `40` | Observations kept per session |
| `max_items` | `60` | Ledger items kept per session. Open items are kept first |
| `reconcile_lines` | `8` | Maximum lines per reconcile section |
| `session_ttl_days` | `30` | Drop records for sessions idle longer than this |
| `client_timezone` | `true` | Use your device zone or `/tz` override before the home zone |
| `device_max_age_hours` | `72` | Ignore a device zone report older than this |

## Your time zone, not the server's

tick shows the time where you are. It picks the first match:

| # | Source | How it arrives |
| --- | --- | --- |
| 1 | You said it | `/tz America/Los_Angeles 5` (5 days), or the agent calls `tick` with `action: tz` when you say where you are. `/tz home` clears it |
| 2 | Your device | The desktop companion reports the zone of the machine the app runs on: at start, every 15 minutes if it changed, on profile switch, and every 6 hours to stay fresh |
| 3 | Home | `timezone` setting, then the Hermes `timezone` setting, then the host zone |

When the zone in use differs from home, the line shows both:

```text
[tick] now Tue 2026-10-13 16:00 PDT (UTC-07:00, your device) | home 19:00 EDT = 2026-10-13T23:00Z
```

The zone is stored per profile, not per session, so every session sees it.

### Desktop companion

The package ships a desktop half (`desktop/plugin.js`) and a small backend
(`dashboard/plugin_api.py`, mounted at `/api/plugins/tick/client`). The
companion is opt-in: turn it on in Capabilities > Plugins. It sends only the
IANA zone name. The backend loads only when `tick` is in `plugins.enabled`.

If the desktop app connects to a remote Hermes host, install the desktop half
on each machine that runs the app. Open this link on that machine. It opens
the install dialog with the repo filled in and only the Desktop target checked:

```text
hermes://plugin-desktop/install?repo=butshouldwe%2Fhermes-plugin-tick
```

Add `&force=1` to replace an older copy. Then turn the companion on and run
**tick: send my time zone now** from the command palette.

On a remote host the desktop half installs from the default branch tip. The
dialog's pin field applies only to the agent half (upstream
[#126591](https://github.com/NousResearch/hermes-agent/issues/126591)).

Example for a game agent:

```yaml
plugins:
  entries:
    tick:
      settings:
        timezone: America/New_York
        stale_after_minutes: 20
        recheck_hint: "Game ticks have advanced. Re-read the realm state before you move."
        observe: ["mcp_*"]
        cutoffs:
          claude-opus-5: "early 2026"
```

## The `tick` tool

| Action | Arguments | Effect |
| --- | --- | --- |
| `add` | `kind` (`decision`, `open`, `check`), `text`, optional `after_minutes` for checks | Adds an item and returns its id |
| `done` | `id` | Closes an item |
| `list` | none | Returns open items |
| `tz` | optional `zone` (IANA or `home`), optional `days` | Sets your zone override, or reports the zone in use |

## Pairs well with Hermes settings

tick tells the agent what is stale. Two Hermes settings cut the cost of a
cold resume:

- `prompt_caching.cache_ttl: auto` keeps a 1-hour cache in human-paced sessions.
- `compression.idle_compact_after_seconds` compacts the history before the
  first reply after a long idle gap, when the cache is cold anyway.

## Develop

```bash
python3 -m pytest -q tests/
node --test 'tests/js/*.test.mjs'
hermes plugins validate .
```

## Credits

The per-turn clock idea also appears in
[hermes-nowstamp](https://github.com/zakiyys/hermes-nowstamp) (MIT). tick is
an independent implementation and shares no code with it.

License: MIT.
