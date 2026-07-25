# Setup

Work through this top to bottom. At the end you'll have a populated `.env`, a deployed Fly.io app, and a working morning brief tomorrow.

Estimated time: ~1–2 hours end to end.

---

## Prerequisites

- macOS or Linux
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) — install with `brew install uv`
- A Fly.io account — sign up at [fly.io](https://fly.io) (free tier is sufficient; needs a credit card on file)
- The Fly CLI — `brew install flyctl`
- Telegram installed on your phone

---

## 0. Create your private training profile

Copy the public example into the ignored local path:

```bash
cp config/training_profile.example.toml config/training_profile.local.toml
```

Edit the local file with your current phase, optional race date, weekly roles,
training principles, and project context. It is intentionally ignored by Git.

Generate the secret used in the Garmin push URL and add it to `.env`:

```bash
openssl rand -hex 32
```

Set the output as `GARMIN_WEBHOOK_SECRET`. Do not reuse an API key or account
password.

---

## 1. Telegram bot

(Easiest, do it first — gives you something to test against immediately.)

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`. Follow the prompts:
   - **Name**: anything (e.g. `Morning Brief`)
   - **Username**: must end in `bot` (e.g. `my_morning_brief_bot`)
3. BotFather replies with a **bot token** like `1234567890:ABCdefGHIjkl...`. Copy it.
   - Set `TELEGRAM_BOT_TOKEN=...` in `.env`.
4. In Telegram, find your new bot and send it any message (e.g. `hi`).
5. In a terminal, fetch your chat_id:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
   In the JSON response, look for `"chat":{"id":<number>...`. That number is your **chat_id**.
   - Set `TELEGRAM_CHAT_ID=<number>` in `.env`.
6. Confirm it works:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage?chat_id=<YOUR_CHAT_ID>&text=hello"
   ```
   Your phone should buzz immediately.

---

## 2. Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign in.
2. Navigate to **API Keys** → **Create Key**. Name it (e.g. `morning-brief-agent`).
3. Copy the key (starts with `sk-ant-...`).
   - Set `ANTHROPIC_API_KEY=...` in `.env`.
4. Make sure your workspace has billing enabled. Daily synthesis is cheap (~$0.01–0.05/day on Sonnet).

---

## 3. intervals.icu API key + athlete ID

1. Sign in at [intervals.icu](https://intervals.icu).
2. Click your avatar (top right) → **Settings**.
3. Scroll to **Developer Settings**. Copy your **API Key**.
   - Set `INTERVALS_ICU_API_KEY=...` in `.env`.
4. Find your athlete ID — it's in the URL when you view your profile (`intervals.icu/athlete/i12345/...`). Include the leading `i`.
   - Set `INTERVALS_ICU_ATHLETE_ID=i12345` in `.env`.
5. Test the credential:
   ```bash
   curl -u "API_KEY:<your-key>" \
     "https://intervals.icu/api/v1/athlete/<your-athlete-id>/wellness?oldest=2026-05-04&newest=2026-05-11"
   ```
   You should get a JSON array back (possibly empty if you have no wellness entries — that's fine).

---

## 4. Google Sheets service account

This section has the most clicks, but you only do it once.

### 4a. Create a Google Cloud project and enable the Sheets API

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Top bar → project dropdown → **New Project**. Name it (e.g. `morning-brief-agent`). Create.
3. Make sure the new project is selected in the top bar.
4. Left menu → **APIs & Services** → **Library**.
5. Search for **Google Sheets API** → click → **Enable**.

### 4b. Create a service account

1. Left menu → **APIs & Services** → **Credentials**.
2. **+ Create Credentials** → **Service account**.
3. **Service account name**: `morning-brief-reader` (or anything).
4. **Service account ID** auto-fills — leave it.
5. Click **Create and Continue**.
6. **Role**: leave blank (Sheets access is granted per-sheet, not project-wide). Continue. Done.

### 4c. Generate a JSON key

1. From the Credentials page, click into the service account you just created.
2. **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**.
3. A JSON file downloads to your machine. **Open it and note the `client_email`** — it looks like `morning-brief-reader@morning-brief-agent.iam.gserviceaccount.com`.

### 4d. Share the Sheet with the service account

1. Open your past-runs Google Sheet in a browser.
2. Click **Share**, paste the `client_email` from step 4c, set permission to **Viewer**, uncheck "Notify people", click **Share**.

### 4e. Capture the Sheet ID and tab name

1. The Sheet ID is in the URL between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`<SHEET_ID>`**`/edit#gid=0`
   - Set `GOOGLE_SHEET_ID=...` in `.env`.
2. The tab name is at the bottom of the Sheet (default is `Sheet1`).
   - Set `GOOGLE_SHEET_TAB=...` in `.env`.

### 4f. Base64-encode the service account JSON

```bash
base64 -i ~/Downloads/morning-brief-agent-<id>.json | tr -d '\n'
```

Copy the output. Set `GOOGLE_SERVICE_ACCOUNT_JSON_B64=<paste>` in `.env`.

**Then delete the downloaded JSON file** — the encoded version in `.env` is enough, and you don't want a duplicate of the secret floating around.

### 4g. Enable Google Health API and create an OAuth client

Use the same Google Cloud project as the Sheets API. This keeps the agent's Google credentials in one place.

1. In [Google Cloud Console](https://console.cloud.google.com), select the same project.
2. Left menu -> **APIs & Services** -> **Library**.
3. Search for **Google Health API** -> click -> **Enable**.
4. Go to **Google Auth Platform**:
   - **Branding**: configure the consent screen. For a personal app, **External** + **Testing** is enough to bootstrap, but Testing-mode refresh tokens expire after 7 days. For ongoing unattended morning briefs, move the app to **In production** once the consent screen is ready.
   - **Audience**: add your own Gmail address as a test user. If you skip this, Google will block login with `Error 403: access_denied`.
   - **Data Access**: add these read-only scopes:
     - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
     - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
     - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
5. Create an OAuth client:
   - **Application type**: `Web application`
   - **Name**: `morning-brief-google-health-local` (console-only; users do not see it)
   - **Authorized redirect URI**: `http://localhost:8080/google-health/callback`
6. Copy the client ID and client secret into `.env`:
   ```bash
   GOOGLE_HEALTH_CLIENT_ID=...
   GOOGLE_HEALTH_CLIENT_SECRET=...
   GOOGLE_HEALTH_REDIRECT_URI=http://localhost:8080/google-health/callback
   ```

Google Health user data from Fitbit Air cannot be read with an API key. It requires OAuth because the data belongs to your Google account and each scope needs user consent.

> Google Health token caveat: while the OAuth consent screen is in **Testing**, Google issues time-limited refresh tokens. Expect to rerun the bootstrap weekly until the app is moved to **In production**.

### 4h. Get your Google Health tokens

Run the one-shot local OAuth flow:

```bash
uv run scripts/bootstrap_google_health_oauth.py
```

What happens:
1. Script opens Google's consent screen with the read-only Health scopes.
2. You sign in with the Gmail account listed as a test user.
3. Google redirects to `http://localhost:8080/google-health/callback`.
4. The script exchanges the authorization code for an access token + refresh token.
5. Tokens are written into local `data/state.db`, next to the Garmin tokens.

Smoke-test that records are visible without printing health values:

```bash
uv run scripts/smoke_google_health.py
```

---

## 5. Garmin Developer Portal app

Prerequisite: you have an approved Garmin Connect Developer Program account. If you don't, apply first (1–4 week approval).

> **OAuth 1 vs OAuth 2.** Garmin migrated to **OAuth 2.0 with PKCE**. New apps are OAuth 2 by default. If your existing app is on OAuth 1 (older credentials use "Consumer Key"/"Consumer Secret" instead of "Client ID"/"Client Secret"), email `connect-support@developer.garmin.com` to migrate before continuing. See `garmin_documentation/OAuth2 Migration Guide.pdf`. OAuth 1 retires 2026-12-31.

1. Sign in to [developerportal.garmin.com](https://developerportal.garmin.com) → **My Apps** (direct URL: `developerportal.garmin.com/user/me/apps?program=829`).
2. **Create a new app**. Name it (e.g. `Morning Brief Agent`) with a short description — this is what appears on the consent screen when you authorize.
3. During app creation, select the **Health API** at minimum. (Activity API optional for v0.)
4. After creation, the app's settings page shows:
   - **Client ID** → set `GARMIN_CLIENT_ID` in `.env`
   - **Client Secret** → set `GARMIN_CLIENT_SECRET` in `.env`
5. Set the **Redirect URI** on the app to `http://localhost:8080/callback` (or whatever you put in `GARMIN_REDIRECT_URI`). This is used only by the one-time bootstrap script in step 6; it doesn't need to be publicly reachable.
6. Your first app's key is an **evaluation-level key** (rate-limited; fine for one user). To upgrade to production-level later, email `connect-support@developer.garmin.com` with the evaluation key.
7. **Don't configure the Push notification URL yet** — that comes in step 8 after the Fly app is deployed.

---

## 6. Get your Garmin tokens (OAuth 2.0 PKCE)

Garmin uses OAuth 2.0 with PKCE. Token lifetimes:
- **Access token: 24 hours.** Sent as `Authorization: Bearer <token>` on API calls.
- **Refresh token: ~90 days.** A new refresh token is issued every time you refresh, so the rolling window persists indefinitely as long as the app refreshes within 90 days.

You run a one-shot script to authorize yourself and capture the initial token pair. The app handles all refreshes automatically from there.

```bash
uv sync
uv run scripts/bootstrap_garmin_oauth.py
```

What happens:
1. Script reads `GARMIN_CLIENT_ID`, `GARMIN_CLIENT_SECRET`, and `GARMIN_REDIRECT_URI` from `.env`.
2. Generates a PKCE `code_verifier` + `code_challenge` (SHA-256), then opens this URL in your browser:
   `https://connect.garmin.com/oauth2Confirm?response_type=code&client_id=...&code_challenge=...&code_challenge_method=S256&redirect_uri=...&state=...`
3. You sign in to Garmin Connect with your own account and approve the permissions.
4. Garmin redirects to `http://localhost:8080/callback?code=<code>&state=<state>`. The script's local listener catches it.
5. Script POSTs the code (plus the original `code_verifier`) to `https://diauth.garmin.com/di-oauth2-service/oauth/token`, receives `access_token` + `refresh_token`.
6. Script calls `GET https://apis.garmin.com/wellness-api/rest/user/id` to capture your stable `userId` — this is what the app uses to match incoming push notifications to you.
7. All four values (access_token, refresh_token, expires_at, userId) are written into local `data/state.db` (SQLite).

You run this **once**, locally. The app rotates tokens in SQLite from there.

> **Redirect URI must match exactly** between the Garmin Developer Portal app settings, your `GARMIN_REDIRECT_URI` env var, and the URL the bootstrap script uses. Mismatch → Garmin rejects the auth request.

---

## 7. Deploy to Fly.io

1. From the repo root:
   ```bash
   fly auth login           # opens browser, sign in / sign up
   fly launch --no-deploy   # detects Dockerfile, asks a few questions
   ```
   - Choose a region close to you (e.g. `sjc` for SF Bay Area, `lhr` for London).
   - **No** when asked about Postgres or Redis.
   - **No** when asked to deploy immediately — we need secrets and the volume first.
2. Push the vars from `.env` to Fly using Fly's stdin-based secret
   import so values are not split by shell expansion:
   ```bash
   fly secrets import < .env
   ```
3. Encode the ignored training profile and set it as a Fly secret. Do this after
   the import so the blank placeholder in `.env` cannot replace it:
   ```bash
   fly secrets set \
     TRAINING_PROFILE_TOML_B64="$(base64 < config/training_profile.local.toml | tr -d '\n')"
   ```
4. Create a 1GB volume for SQLite (this is where OAuth tokens live, so the app can persist token rotations):
   ```bash
   fly volumes create data --size 1 --region <your-region>
   ```
5. Deploy:
   ```bash
   fly deploy
   ```
6. **Upload your bootstrapped OAuth tokens to the Fly volume.** The bootstrap scripts wrote them to your local `data/state.db`; we now copy that file onto Fly:
   ```bash
   fly ssh sftp shell
   sftp> put data/state.db /data/state.db
   sftp> quit
   fly apps restart           # so the app re-reads tokens
   ```
7. Get your app's URL:
   ```bash
   fly status
   ```
   Note the hostname (e.g. `https://my-morning-brief.fly.dev`). You'll register this with Garmin next.
8. Sanity check:
   ```bash
   curl https://<your-app>.fly.dev/health
   ```
   Should return `200 OK`.

---

## 8. Register the Push URL with Garmin

We use Garmin's **Push** service rather than Ping — push delivers the full sleep summary as JSON inside the POST body, no callback fetch required.

1. Open the Garmin **Endpoint Configuration Tool**: [apis.garmin.com/tools/endpoints](https://apis.garmin.com/tools/endpoints/). Sign in with your `GARMIN_CLIENT_ID` and `GARMIN_CLIENT_SECRET`.
2. Find the **Sleep** summary type. Configure:
   - **URL**: `https://<your-app>.fly.dev/garmin/push/<GARMIN_WEBHOOK_SECRET>`
   - **Type**: **Push** (not Ping)
   - **Enabled**: checked
3. Save. Garmin may flag the new domain for a security review that auto-clears in 24–48h. If you need it sooner, email `connect-support@developer.garmin.com` to fast-track domain validation.
4. **Optional but recommended:** also enable Push for `userMetrics` (HRV, VO2 max) and `dailies` (steps, resting HR, Body Battery) for richer brief context. The handler routes by summary type.

> The secret is part of the URL because Garmin's endpoint tool does not provide
> a custom authentication-header field. The handler also rejects entries whose
> `userId` does not match the bootstrapped Garmin account.

> The Push handler must respond `200` within 30 seconds or Garmin counts it as failed and retries with backoff. Heavy work (Claude call, intervals.icu fetch, Telegram send) runs asynchronously after the ack — we never block the response on it.

---

## You're live

Sleep on your watch tonight. When you wake and the watch syncs (typically 1–2 minutes after wake), your phone should buzz with the morning brief within seconds.

### If it doesn't fire

```bash
fly logs --since 1h
```

Look for:
- The incoming `POST /garmin/push/<secret>` request — did Garmin actually fire?
- Any errors fetching from intervals.icu or Sheets, or in the Claude synthesis.
- The outbound Telegram call — did it return 200?

Run the pipeline manually without a real ping:
```bash
uv run scripts/send_test_brief.py
```

This fires the full synthesis + delivery using whatever data is currently in Garmin/intervals/Sheets. Good for debugging the pipeline without waiting for tomorrow morning.

---

## Common issues

| Symptom | Likely cause |
|---|---|
| Telegram never receives a message | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` wrong; check with `curl` in step 1.6 |
| Garmin Endpoint Config shows security-review warning | Auto-clears in 24–48h; or email `connect-support@developer.garmin.com` to fast-track |
| `403` on Sheets read | Service account not given Viewer access on the Sheet (step 4d) |
| `401` on intervals.icu | Wrong API key, or athlete ID missing the `i` prefix |
| `401` on Garmin API calls in logs | Access token expired and refresh failed — re-run `bootstrap_garmin_oauth.py` locally, then re-upload `data/state.db` to Fly via `fly ssh sftp` (deployment step 6) |
| No push by 8am | Watch didn't sync — usually fixes itself once you open the Garmin Connect app on your phone |

---

## Editing the brief itself

The system prompt is in [`prompts/system.md`](./prompts/system.md). Edit the markdown, commit, `fly deploy` (~30s). Next ping uses the new prompt.

The brief is read at request time, not import time, so no service restart needed beyond the deploy itself.
