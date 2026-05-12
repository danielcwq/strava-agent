# Morning Brief Agent

A daily training brief that fires when your Garmin syncs in the morning. Pulls sleep, training load, planned workout, and recent run history; synthesizes a short brief with Claude; delivers via Telegram.

```
Watch wakes you ──► Garmin Connect ──► /garmin/push (this app)
                                       (full sleep JSON in POST body)
                                              │
                                              ├─► intervals.icu (CTL/ATL/TSB + today's plan)
                                              └─► Google Sheets (past runs, lap-level)
                                                       │
                                                       ▼
                                                   Claude
                                                       │
                                                       ▼
                                                  Telegram ──► your phone
```

Garmin uses **OAuth 2.0 PKCE** and **Push** (full data in POST body, not Ping + callback). Access tokens expire every 24h and refresh tokens every ~90d; the app rotates both automatically in SQLite.

## Stack

- Python 3.12, FastAPI
- SQLite (OAuth tokens, ping dedup) on a Fly volume
- Anthropic Claude (synthesis)
- Fly.io (always-on host)
- Telegram Bot API (delivery)

## Setup

Full credential acquisition + deploy walkthrough: see **[SETUP.md](./SETUP.md)**.

## Customizing the brief

The system prompt is plain markdown at [`prompts/system.md`](./prompts/system.md). Edit, `fly deploy`, done. The prompt is loaded per-request so no restart is needed beyond the deploy itself.

## Local testing

```bash
uv sync
cp .env.example .env  # fill in per SETUP.md
uv run scripts/send_test_brief.py
```

Fires the full pipeline (fetch → synthesize → Telegram) end-to-end without needing a real Garmin ping.

## Project layout

```
strava-agent/
├── SETUP.md                   # Step-by-step credential + deploy walkthrough
├── prompts/
│   └── system.md              # Coach persona + output format (edit freely)
├── src/
│   ├── main.py                # FastAPI receiver: /garmin/push, /health
│   ├── config.py              # Env loading + validation
│   ├── db.py                  # SQLite: OAuth tokens, ping dedup
│   ├── clients/               # Garmin, intervals.icu, Sheets, Claude, Telegram
│   ├── synthesis.py           # Build context → call Claude → parse response
│   └── pipeline.py            # morning_brief() orchestrator
└── scripts/
    ├── bootstrap_garmin_oauth.py    # One-shot OAuth 1.0a flow (run once)
    └── send_test_brief.py           # Fire the pipeline manually
```
