# Design notes

Why tick works the way it does. Each invariant below has a test that fails
without it.

## Goal

Every agent knows the current time in the user's zone, how long the user was
away, and what it observed before the gap. After a long gap or a compaction,
it re-checks stale state (pull requests, CI, deploys, external systems)
before acting on it. Every behaviour is configurable; no use case is a
default.

## Decisions

| Decision | Why | Rejected |
| --- | --- | --- |
| Inject through `pre_llm_call` into the current user message only | The prompt cache stays valid; history rewrites break it | Replacing the context engine |
| Observations come from `post_tool_call`, with no LLM call | Zero token cost, deterministic | Asking the model to log everything |
| Detect compaction with `pre_auxiliary_call` when `aux_task` contains `compress` | The only public signal; observer-only hook | Patching the compressor (core rebind) |
| Ledger lives in `ctx.state`, per session | Profile-scoped, survives restarts, public API | Reading `state.db` |
| Serialize each session read-modify-write (thread lock plus `fcntl` lock file) | `ctx.state` get and set are atomic one at a time, not as a sequence | Per-key atomicity only |
| Item ids come from one profile-wide counter; inheritance merges without renumbering | A child session may hold entries before its first turn, and an id the agent has seen must never change meaning | Replacing the child with the parent; renumbering child ids |
| Hashed session keys use the `h:` prefix, raw keys `s:` | Disjoint namespaces, so no alias | An `s:h:` prefix |
| `enabled: false` also removes the tool and `/tz`, and both refuse at call time | A master switch covers all behaviour | Gating hooks only |
| Zone resolution: override, then fresh device report, then home | An explicit statement beats inference; the device beats a static default | Device only (fails on messaging and terminal surfaces) |
| The desktop half reports the `Intl` zone to the plugin backend, which writes `PluginState('tick')` | Same store the hooks read; public surfaces only | Writing `config.yaml`; an upstream per-turn client zone |
| The desktop half is opt-in and sends only the IANA zone name | Matches the desktop app's privacy default | Default-on |
| Delivery is tracked per destination profile; a failure leaves no record | `ctx.rest` is profile-aware; success on one profile proves nothing about another | One global last-sent cache |
| Provenance (`resolve_zone`) is separate from display (`client_zone`) | An override equal to home still reports `override` | One function for both |
| Each zone candidate is validated on its own; expiry must be finite | A bad override never hides a valid device record; state stays strict JSON | One shared try block |
| The previous-message gap skips the trailing current user message | Hermes passes history that already ends with the current message | Reading the last history entry |

## Invariants

- Injected text appears only in the hook's return value; history is untouched.
- Every hook returns `None` or a context dict, and never raises.
- Concurrent observations and adds in one session all persist, with unique ids.
- A rotated session keeps both parent and pre-first-turn child entries;
  inheritance runs once.
- An id returned by `add` always names the same item, across sessions and
  inheritance.
- Every `DEFAULTS` key is in `config_schema` with the same default, and in the
  README.
- A malformed higher-priority zone record never hides a valid lower one.
- No non-finite expiry reaches state; state serializes with `allow_nan=False`.
- A failed report for one profile is retried on the next check even if another
  profile succeeded.
- `client_timezone: false` ignores reports and overrides and does not register
  `/tz`.
- The backend never writes on invalid input and shares `DEVICE_KEY` with the
  hooks.

## Known limits

- Ledger-seeded compaction summaries need a hook that can add text to the
  compression prompt. Hermes has none, so tick re-grounds on the first turn
  after compaction instead.
- On a remote backend, the desktop install dialog clones the desktop half at
  the default branch tip; its pin field applies to the agent half only
  ([hermes-agent#126591](https://github.com/NousResearch/hermes-agent/issues/126591)).
