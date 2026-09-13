# Context revamp

Work on `context-revamp`; deliver stages 1–4 through PR #4 and deploy for testing.

## Stage 1 — Durable tracing
- [x] Add sessions, runs, ordered events, and preserve legacy history.
- [x] Persist inbound updates before acknowledgement; deduplicate and serialize chat work.
- [x] Record complete model/tool activity, commands, errors, briefs, and Telegram delivery.
- [x] Preserve archives on `/reset`; identify interrupted/uncertain runs after restart.
- [x] Export private HTML/JSON traces; provide consistent SQLite backup and restore verification.

## Stage 2 — Request context
- [x] Replace the 24-hour / 20-row cutoff with a configurable token budget.
- [x] Include complete exchanges, source-linked versioned summaries, and history search.
- [x] Preserve raw events and reset boundaries; snapshot each actual model request.

## Stage 3 — Editable coaching profile
- [x] Seed a versioned SQLite profile from the existing private TOML exactly once.
- [x] Add explicit schedule, race-goal, and coaching-instruction tools with audited patches.
- [x] Share dynamic profile context across chat, briefs, and `/context`.
- [x] Remove fixed Tuesday / half-marathon assumptions from generic prompts.

## Verification and delivery
- [x] Test failures, duplicate updates, restart recovery, reset isolation, complete context,
      profile validation/authorization, preservation, and chat/brief consistency.
- [x] Document operation, migration, limitations, and trace/backup access.
- [x] Run repository checks, review the diff, commit, push, and open a PR.

Delivered in [PR #4](https://github.com/danielcwq/strava-agent/pull/4).
Validation: 50 automated tests, Ruff, and diff whitespace checks pass.
Deployment and live acceptance results are recorded in PR #4.

## Stage 4 — Fable 5.1
- [x] Verify model access and configure chat, summaries, and briefs.
- [x] Native structured briefs with configurable output allowances.
- [x] Preserve signed reasoning in traces while rebuilding compatible request context.
- [x] Add migration regression tests and deployment revision identification.

## Deferred — do not implement in this PR
- [ ] Stage 5: evaluate an external harness using **Fable 5.1** where supported;
      compare against the simple runtime before adopting Prime Agent / Claude Agent SDK
      or another integration. Use expiring credits when this stage is authorized.
