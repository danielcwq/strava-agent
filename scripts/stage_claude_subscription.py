"""Stage the owner's headless Claude login on Fly without echoing credentials."""

import getpass
import subprocess


def main() -> None:
    token = getpass.getpass("Paste the token from claude setup-token (hidden): ").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("A single nonempty token is required.")
    secrets = (
        "MODEL_BACKEND=claude_code\n"
        "CLAUDE_CODE_USE_LOCAL_LOGIN=false\n"
        f"CLAUDE_CODE_OAUTH_TOKEN={token}\n"
    )
    subprocess.run(
        ["fly", "secrets", "import", "--stage", "-a", "strava-agent"],
        input=secrets,
        text=True,
        check=True,
    )
    print("Subscription credentials staged. The next deployment activates them.")


if __name__ == "__main__":
    main()
