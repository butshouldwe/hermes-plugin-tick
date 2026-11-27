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
| Ledger lives in one file per session under the plugin data dir (0.3.0) | Each `ctx.state` write rewrites the entire store. With thousands of sessions, writes queued under one profile lock and tripped the gateway watchdog. | One `ctx.state` key per session (0.2.x); a host API change |
| Serialize each session read-modify-write: a thread lock plus one of 64 striped `fcntl` lock files, every wait bounded at 5 s | Sessions no longer wait on each other; a slow holder makes hooks fail open instead of queueing past the host's 30 s hook limit | One profile-wide lock file with a blocking wait (0.2.x) |
| The item-id counter (`ids.json`) is the only memory of ids that gc or `max_items` removed. It is seeded once, from `ids:next`, and `ids.seeded` records that it was written. After that, a missing, malformed or unreadable counter refuses the add until an operator sets it | An id the agent has seen is never handed out again | Rebuilding from retained records (cannot see removed ids); reseeding from `ids:next` (stale after the first add) |
| Migrate once at register; a record already in `sessions/` wins | A session that wrote first is never overwritten; a failed run repeats on the next start | Lazy per-session fallback reads (a full store parse on every new session) |
| Client-zone reads are cached on the store file's (inode, mtime, size) | Every turn resolves the zone, and each `get` parses the whole store | Reading on every turn |
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
- No hook reads or writes a session record in `ctx.state` when the host state
  has a `data_dir`; no lock wait exceeds `LOCK_WAIT_S`.
- gc never runs under a session lock, and re-reads each candidate under its
  own lock before deleting it.
- Inheritance names its parents in `inherit_from`. A parent that fails to
  read stays listed and a later turn retries it, without the host repeating
  `parent_session_id`. A parent still waiting on its own ancestors hands them
  on, so rotating while pending keeps the grandparent's obligations.
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

## Id counter fault table

Each row is pinned by a test that fails on the code before its fix.

| State | Fault | Behavior |
| --- | --- | --- |
| `ids.json` | Missing, no `ids.seeded`, migration incomplete | Seed once from `ids:next` and every 0.2.x record, then write `ids.seeded` |
| `ids.json` | Missing after `ids.seeded` (lost, deleted, restored) | Refuse; migration and later adds never reseed |
| `ids.json` | Missing after migration completed | Refuse |
| `ids.json` | Malformed or unreadable | Refuse; the file is left for an operator |
| `ids.json` | Set by an operator after a refusal | Used; the first add writes `ids.seeded` |
| `ids.seeded` | Unreadable (any error but a missing file) | Refuse |
| `ids.seeded` and `ids.json` | Both lost | Read as a new store: seeded from `ids:next`. Accepted: the same as losing the plugin directory |
| `ids:next` (0.2.x) | Malformed at migration | Nothing is seeded; adds refuse until an operator sets `ids.json` |
| `ids:next` (fallback) | Missing after `ids:seeded` | Refuse |
| `ids:next` (fallback) | Missing, no `ids:seeded`, the store file or the session's own record holds an item id | Refuse (a store from before the marker) |
| `ids:next` (fallback) | Missing, no `ids:seeded`, no item id anywhere it can read | Start at 1 |
| `ids:next` (fallback, no store file) | Missing, no marker, ids only in other sessions | Starts at 1. Accepted: a state with neither `data_dir` nor `path` cannot be listed. No host tick supports ships one |
