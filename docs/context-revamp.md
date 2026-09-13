# Context, tracing, and the editable coaching profile

This implements stages 1–4 in [TODO.md](../TODO.md). Chat, history summaries,
and briefs use Fable 5.1. External-harness evaluation remains deferred.

## Storage and ownership

The application owns the archive independently of the model provider. Everything
below lives in the existing SQLite file (`DATA_DIR/state.db`; `/data/state.db` on
Fly). No new hosted database or public trace endpoint is introduced.

| Table | Purpose |
| --- | --- |
| `chat_sessions` | Active/closed conversation boundaries per chat |
| `agent_runs` | One accepted request/brief, input, dedup key, lifecycle status |
| `run_events` | Ordered inputs, model request/response snapshots, tool activity, commands, deliveries, failures |
| `context_summaries` | Versioned older-dialogue notes and their source run IDs |
| `profile_revisions` | Complete coaching profile revisions with the originating input event and instruction |
| `conversation_turns` | Retained, read-only legacy history after migration |

Existing Garmin, OAuth-token, brief-log, and usage tables remain. Fields are stored
without application-level encryption. Anyone who can read the volume or a backup
can read the data, including credentials in the token tables. Trace payloads remove
known configured credentials and credential fields; they still contain personal
messages and health data. File access remains an operator responsibility.

Keep the app as **one process on one Fly Machine using its volume**. The queue is
single-consumer and serializes commands, chat and automatic briefs; it is not a
distributed worker system. Do not enable multiple Uvicorn workers or run another
copy against the live volume. A future distributed deployment needs worker leases
and explicit ownership/recovery semantics.

## Migration and recovery

On startup the application imports existing `conversation_turns` once. It leaves
the old table untouched and labels imported runs `legacy_incomplete`: tool steps
that were never saved cannot be reconstructed. Original timestamps are preserved.

Accepted Telegram text updates are committed before acknowledgement and keyed
by chat/message ID. A retry cannot execute the same message twice. Edits and
non-text updates are currently ignored, as before. The original Telegram update
ID is retained in the input event. Garmin sleep deduplication and queue insertion
are committed together; repeated sleep pushes still refresh stored Garmin data.

Queued requests survive restart. Runs that were executing at restart become
`interrupted`, with their partial traces retained. They are **not automatically
replayed**: a tool mutation or Telegram send may already have succeeded. Review
the trace and current profile before asking the bot to perform the action again.

Delivery events distinguish attempted, accepted, rejected and uncertain chunks.
An accepted Telegram response means the API accepted the message, not that it was
read. A timeout or process death between send and recording acceptance is
ambiguous. Exactly-once external delivery is not claimed.

The archive is append-only during normal use. `/reset` closes the current session
and starts a new one without deleting history or the profile. Historical deletion
is not implemented in this change. No automatic expiry or pruning is configured;
monitor volume usage as the archive and daily backups grow.

## What the model sees

Each chat request includes the base prompt, a current profile snapshot, recent
complete exchanges, and (when needed) a versioned summary of older dialogue. There
is no 24-hour cutoff. The budget uses a conservative UTF-8-byte token estimate:
this is intentionally an upper estimate for the text-only payload, not the
provider's exact tokenizer. Default `CONTEXT_TOKEN_BUDGET=48000` and
`CONTEXT_SUMMARY_TOKENS=3000` can be configured through environment settings.

Whole exchanges are retained together so tool calls/results and signed provider
blocks remain paired. Failed exchanges are represented in normal context by the
user question and an explicit failure note. Their original partial messages are
still available through history retrieval and trace export. A current request or
tool result that exceeds the input allowance fails visibly rather than silently
cutting a tool result in half.

Older exchanges are summarized with source run IDs. Large source exchanges use
labeled text excerpts for summarization; their original records remain intact.
If summarization fails, labeled source excerpts are retained as a bounded fallback.
These notes may be lossy and are never authoritative profile changes. Summarizer
requests/responses are traced and counted in daily usage. Provider-internal
reasoning that the API does not return cannot be archived.

The bot can search original messages by literal keywords or UTC date range and
read complete source exchanges by run ID. Searches default to the active session.
After `/reset`, older sessions are consulted only for an explicit request about
previous conversations. No vector database or embedding pipeline is needed.

## Editing your profile

The first profile read imports your existing private TOML (or Fly secret) exactly
once, preserving the prose and schedule. Afterwards SQLite revisions are the
source of truth: redeploying with an old secret does not overwrite bot edits.
Keep the original private profile as a seed/recovery reference. Race fields are
stored separately from schedule roles in the same document.

Example Telegram messages:

- `Move track to Wednesday from now on and make Tuesday easy.`
- `Save my next race date as 2026-10-18, with a target of 1:25.`
- `Update my coaching instructions: add that consistency matters more than mileage.`
- `/context` shows the complete saved profile and its revision.
- `/reset` starts fresh conversation context while preserving the profile.

Profile writes require your private Telegram chat (sender ID must match the
configured personal chat ID); group chats cannot edit the profile.
The model extracts a narrow patch; the server validates the active owner's run,
the exact originating message, allowed fields, dates/roles, and expected profile
revision. Clear imperative requests and explicit race/goal declarations are
accepted. A conservative English-language intent gate rejects hypothetical,
negated, quoted, or ambiguous phrasing; the bot asks for a clear save instruction
when necessary. This is a bounded personal bot interface, not a general natural
language authorization system. Unrelated fields remain unchanged. Changes and
the source instruction are committed atomically in the revision history.

Temporary schedule exceptions such as `this week only` are not implemented and
must not overwrite the recurring schedule. Ask about the exception conversationally.
The bot should clarify a move when the old day's replacement role is unspecified.

Chat, `/context`, and **every day's** brief use the current profile. Within a brief,
data and prompts use one consistent revision. Explicit saved structured fields
override conflicting race dates or schedule assumptions in imported prose. The
generic prompt no longer treats Tuesday or a half-marathon phase as fixed facts.

## Inspect traces privately

Run these against a local database or a consistent downloaded backup:

```sh
uv run python scripts/state_archive.py --db data/state.db list
uv run python scripts/state_archive.py --db data/state.db export RUN_UUID
```

The export creates JSON and an escaped, self-contained HTML timeline under
`private-artifacts/`, which is ignored by Git. Files have owner-only permissions.
There are no external assets, scripts, or public upload step. Use a new output
directory when exporting the same run again; existing files are never overwritten.

The application logs include run IDs; after Fly login, `fly logs -a strava-agent`
shows runtime failures. The database/export is the detailed record. The trace
contains actual assembled prompts and model settings, so it can explain what
information the model had for an answer.

## Backups and restore verification

The worker creates a consistent daily SQLite backup at
`/data/backups/state-YYYY-MM-DD.db`, using SQLite's online backup API to include
committed WAL contents. It checks integrity before retaining the file. Backup
failure is logged and does not stop request processing. Backups on the same
volume protect against accidental database changes, **not volume loss**; copy
them off the Machine for disaster recovery. They contain OAuth credentials too.

For a manual backup and independent restore verification:

```sh
uv run python scripts/state_archive.py --db data/state.db backup private-artifacts/backup.db
uv run python scripts/state_archive.py --db private-artifacts/backup.db restore private-artifacts/restored.db
uv run python scripts/state_archive.py --db private-artifacts/restored.db verify
```

`backup` and `restore` both use SQLite's backup API and refuse to overwrite any
existing destination. Verification opens a restored copy, checks database and
foreign-key integrity, parses event JSON, and reports counts without printing
message or token contents. The unit tests verify run IDs survive restoration.

On Fly, download a completed daily backup through `fly ssh sftp shell` (for example,
`get /data/backups/state-YYYY-MM-DD.db ./backup.db`) rather than copying the live
`state.db` alone while WAL writes are active. To restore production, stop all app
writers, keep the current database and its WAL files as a rollback copy, then
replace the stopped database with the verified restored file. Do not upload an
old local `state.db` over production during a normal redeploy: it would replace
the live archive and profile revisions.

## Verification scope

Automated tests use temporary databases, synthetic messages and mocked provider/
Telegram responses. They exercise persistence, failures, deduplication, recovery,
reset isolation, complete exchanges, summary sources, authorization/validation,
profile consistency, export escaping, and backup/restore. They do not claim a live
Fly deployment, real Telegram delivery, or model interpretation evals. Deployment
and a small real-world acceptance check remain a release step after PR review.


## Stage 4: model and structured briefs

The default API identifier is `claude-fable-5-1`, configurable separately through
`CHAT_MODEL` and `BRIEF_MODEL`. Chat uses high effort, briefs medium, and history
summaries low. Adaptive thinking is enabled. Changing models requires checking
that the replacement supports this request format.

Briefs use native JSON schema output (`headline`, `body`, `flags`) and strict
validation before delivery. Refused, incomplete, and malformed responses fail the
run and remain inspectable in its trace. There is no 1,500-token brief ceiling.
The configurable output allowances are 32,768 for chat, 16,384 for briefs, and
8,192 for summaries, including reasoning. The prompt still asks for a short
Telegram brief. Requests over 16,000 output tokens use streaming transport and
archive the complete response before returning it. A failed stream records an
error; unfinished streamed fragments are not yet persisted.

Signed reasoning blocks stay in the raw archive. Request replay removes prior
reasoning when rebuilding the profile/date/summary prefix; the active tool loop
preserves it until that prefix changes (for example after a profile edit).
This avoids replaying signatures bound to an obsolete context.

`/usage` reports model request counts and tokens for chat and summaries. It excludes
briefs and no longer applies obsolete Sonnet dollar rates to mixed-model history.
Consult Anthropic Console for billing and credit balances.

Deploy a tested commit with `fly deploy --remote-only --build-arg APP_REVISION=<sha>`.
`/health` reports the embedded revision. The image includes `scripts/state_archive.py`
for private trace export and consistent backups; it is not a public trace viewer.

Historical exchanges are compacted in batches of at most 24,000 estimated material
tokens (lower when the configured context budget requires it), with source IDs and
a durable checkpoint per batch. The initial import therefore needs a few summary
requests instead of one per old exchange. Completed batches survive a restart.


## Reading saved preferences in Telegram

`/context` shows a compact, formatted overview of the training phase, weekly
schedule, and race goal. `/context instructions`, `/context background`, and
`/context race` show the complete corresponding saved sections. Long sections
include a next-page command (for example `/context instructions 2`). Text is
escaped and each page is rendered as a complete Telegram HTML message. These
views do not alter the saved profile or the context supplied to the model.


Tool loops re-check the request allowance after each result. If it grows too
large, they remove prior reasoning signatures and whole older exchanges from the
request replay, preserving the current user message and active tool-call pairing.
`context.compacted` records the before/after estimates; originals remain in the
archive. An active turn that cannot fit by itself fails explicitly with a
`context.overflow` event instead of silently truncating instructions or tool data.
Worker error traces also include the credential-redacted exception message.


## Stress-tested tool context

Active requests use the previous provider response's total input usage (including
cache reads/writes) as their baseline. Unchanged messages retain that measured
cost; changed messages are conservatively charged by serialized bytes plus
framing. Prefix changes invalidate this measurement and fall back to the byte
bound. `context.request_budget` records both estimates. The initial history
selection still uses the conservative byte bound, independently of archive retention.

Read-only tool responses over 12 KB become bounded views with durable event IDs.
`read_tool_result` can inspect exact JSON-pointer paths and page through arrays,
objects, or strings. Page coverage and omitted children are explicit. A growing
active turn may replace older read-result bodies with these references while
preserving tool IDs, save acknowledgements, errors, and assistant evidence notes.
Original tool results remain in `tool.finished` events. Retrieval enforces chat
ownership and session boundaries; older sessions still require an explicit request.

The opt-in `scripts/stress_agent.py` suite uses a NEW directory and a consistent
backup. It makes real model/read-only data calls, never sends Telegram messages,
and checks that read-only questions cannot change the profile. Coaching and
schedule edit cases affect only the database copy. Example:

```sh
PYTHONPATH=. uv run python scripts/stress_agent.py \
  --db private-artifacts/state-backup.db \
  --output private-artifacts/new-stress-run \
  --allow-model-calls
```

Its `report.json` and SQLite trace are private artifacts. The automated suite also
covers 24-tool chains, large parallel fetches, lossless paging, Unicode, malformed
page arguments, scope boundaries, mutation preservation, usage caching, and
message changes. The September 13 validation passed 84 automated tests and seven
live-model scenarios (analysis, evidence follow-up, wellness, history recall,
coaching edit, schedule edit, and saved-state follow-up), plus two focused analysis
reruns. An exact replay of a previously failed nine-tool request completed with a
60,405-byte serialized request and a conservative 38,687-token input estimate,
below the unchanged 48,000-token allowance. No production preferences were edited
by these isolated tests. These checks exercise runtime reliability and data coverage;
race forecasts remain uncertain coaching interpretations rather than guarantees.
