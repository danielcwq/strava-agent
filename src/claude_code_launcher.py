#!/usr/bin/env python3
"""Run the SDK's CLI with only the environment needed for subscription auth."""

import os
import sys

PASSTHROUGH = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
)


def main() -> None:
    binary = os.environ["COACH_CLAUDE_BINARY"]
    env = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    os.execve(binary, [binary, *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
