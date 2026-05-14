"""Register, inspect, or delete the Telegram inbound webhook.

Examples:
    uv run scripts/register_telegram_webhook.py --info
    uv run scripts/register_telegram_webhook.py --url https://strava-agent.fly.dev/telegram/webhook
    uv run scripts/register_telegram_webhook.py --delete

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from .env.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src.config import settings  # noqa: E402

API_BASE = "https://api.telegram.org"


def _api(method: str, body: dict | None = None) -> dict:
    r = httpx.post(
        f"{API_BASE}/bot{settings.telegram_bot_token}/{method}",
        json=body or {},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def set_webhook(url: str, secret_token: str) -> dict:
    return _api(
        "setWebhook",
        {
            "url": url,
            "secret_token": secret_token,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
    )


def delete_webhook() -> dict:
    return _api("deleteWebhook", {"drop_pending_updates": True})


def get_webhook_info() -> dict:
    return _api("getWebhookInfo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="Webhook URL to register (e.g. https://.../telegram/webhook)")
    parser.add_argument("--delete", action="store_true", help="Delete the current webhook")
    parser.add_argument("--info", action="store_true", help="Show current webhook info and exit")
    args = parser.parse_args()

    if args.info:
        print(json.dumps(get_webhook_info(), indent=2))
        return 0

    if args.delete:
        print(json.dumps(delete_webhook(), indent=2))
        return 0

    if not args.url:
        parser.error("--url required (or use --delete / --info)")

    if not settings.telegram_webhook_secret:
        print("ERROR: TELEGRAM_WEBHOOK_SECRET must be set in .env first.")
        print("       Generate one with: openssl rand -hex 32")
        return 1

    print(f"Registering webhook URL: {args.url}")
    result = set_webhook(args.url, settings.telegram_webhook_secret)
    print(json.dumps(result, indent=2))
    print()
    print("Current webhook info after registration:")
    print(json.dumps(get_webhook_info(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
