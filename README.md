# Morning Brief Agent

A daily training brief that fires when your Garmin syncs in the morning. Pulls sleep, training load, planned workout, and recent run history; synthesizes a short brief with Claude; delivers via Telegram.

```
Watch wakes you ──► Garmin Connect ──► /garmin/push/{secret} (this app)
                                       (full sleep JSON in POST body)
                                              │
                                              ├─► intervals.icu (CTL/ATL/TSB + today's plan)
                                              ├─► Google Sheets (past runs, lap-level)
                                              └─► Google Health API (Fitbit corroboration)
                                                       │
                                                       ▼
                                                   Claude
                                                       │
                                                       ▼
                                                  Telegram ──► your phone
```

Garmin uses **OAuth 2.0 PKCE** and **Push** (full data in POST body, not Ping + callback). Access tokens expire every 24h and refresh tokens every ~90d; the app rotates both automatically in SQLite.

Google Health uses **OAuth 2.0** for read-only Fitbit Air data. The brief treats it as a secondary source for sleep, weight, body composition, HRV, resting heart rate, oxygen saturation, respiratory rate, active-zone minutes, and exercise.

## Stack

- Python 3.12, FastAPI
- SQLite (OAuth tokens, ping dedup) on a Fly volume
- Anthropic Claude (synthesis)
- Fly.io (always-on host)
- Telegram Bot API (delivery)

## Setup

Full credential acquisition + deploy walkthrough: see **[SETUP.md](./SETUP.md)**.

## Private training profile

The repository contains only
[`config/training_profile.example.toml`](./config/training_profile.example.toml).
Copy it to `config/training_profile.local.toml` and add your race, schedule,
training principles, and coaching context there. The local file is excluded from
Git and Docker build contexts. For Fly, provide the same content through the
`TRAINING_PROFILE_TOML_B64` secret.

The general coach prompt remains in [`prompts/system.md`](./prompts/system.md).

## Security and privacy

- Garmin push requests use a per-deployment secret URL and must contain the
  bootstrapped Garmin user ID before any payload is stored.
- OAuth tokens, Garmin summaries, generated briefs, and Telegram conversation
  history are retained in plaintext SQLite on the configured data volume.
- Sleep, recovery, workout, and profile context is sent to Anthropic to generate
  briefs and chat responses. Data also flows through the Garmin, Google,
  intervals.icu, Telegram, Fly.io, and Google Sheets services you configure.
- This is a personal single-user project, not a multi-user health platform.

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
├── config/
│   └── training_profile.example.toml  # Public template; copy to ignored .local.toml
├── prompts/
│   └── system.md              # Coach persona + output format (edit freely)
├── src/
│   ├── main.py                # FastAPI receiver: secured Garmin push + /health
│   ├── config.py              # Env loading + validation
│   ├── db.py                  # SQLite: OAuth tokens, ping dedup
│   ├── clients/               # Garmin, intervals.icu, Sheets, Claude, Telegram
│   ├── synthesis.py           # Build context → call Claude → parse response
│   └── pipeline.py            # morning_brief() orchestrator
└── scripts/
    ├── bootstrap_garmin_oauth.py    # One-shot Garmin OAuth 2.0 PKCE flow
    ├── bootstrap_google_health_oauth.py  # One-shot Google Health OAuth flow
    ├── smoke_google_health.py       # Check Google Health data availability
    └── send_test_brief.py           # Fire the pipeline manually
```
