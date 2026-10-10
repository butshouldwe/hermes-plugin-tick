# Context

- **Turn**: one user message and the agent loop that answers it.
- **Gap**: wall-clock time between the previous message in a session and now.
- **Stale**: a gap longer than the configured threshold; earlier observations
  of mutable state may no longer hold.
- **Ledger**: per-session record of observations (tool calls with time and
  outcome) and commitments (decisions, open items, next checks).
- **Reconcile**: after a stale gap or a compaction, re-show the ledger entries
  that may have changed so the agent re-checks before acting.
