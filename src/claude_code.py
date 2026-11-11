"""Claude Agent SDK backend for the bot owner's subscription."""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4
from zoneinfo import ZoneInfo

import claude_agent_sdk as sdk

from src import archive, db, tool_context
from src.config import settings

_launcher = Path(__file__).with_name("claude_code_launcher.py")
_process_lock = Lock()


class ClaudeCodeError(RuntimeError):
    pass


def _session_env(directory: Path) -> dict[str, str]:
    token = settings.claude_code_oauth_token
    bundled_cli = Path(sdk.__file__).parent / "_bundled" / "claude"
    env = {
        "COACH_CLAUDE_BINARY": str(bundled_cli),
        "CLAUDE_CONFIG_DIR": str(directory / "config"),
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "HOME": str(directory),
    }
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.get_secret_value()
    elif settings.claude_code_use_local_login:
        env["HOME"] = str(Path.home())
        env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
        env["COACH_CLAUDE_BINARY"] = shutil.which("claude") or str(bundled_cli)
    else:
        raise ClaudeCodeError("Claude Code setup-token is missing")
    return env


def _verify_subscription(env: dict[str, str], directory: Path) -> None:
    try:
        result = subprocess.run(
            [str(_launcher), "auth", "status"],
            cwd=directory,
            env={**os.environ, **env},
            capture_output=True,
            check=True,
            timeout=20,
        )
        auth = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ClaudeCodeError("Could not verify Claude Code subscription authentication") from None
    expected = "oauth_token" if env["CLAUDE_CODE_OAUTH_TOKEN"] else "claude.ai"
    if (
        not isinstance(auth, dict)
        or auth.get("loggedIn") is not True
        or auth.get("authMethod") != expected
        or auth.get("apiProvider") != "firstParty"
    ):
        raise ClaudeCodeError("Claude Code did not report subscription authentication")


def _sdk_tool(spec: dict, execute: Callable, run_id: str):
    name = spec["name"]

    async def handler(args: dict) -> dict:
        with archive.bind(run_id):
            call_id = str(uuid4())
            archive.event("tool.started", {"id": call_id, "name": name, "input": args})
            result = execute(name, args)
            event_id = archive.event(
                "tool.finished", {"id": call_id, "name": name, "result": result}
            )
            prompt_result = tool_context.for_prompt(event_id, name, result)
        return {
            "content": [{"type": "text", "text": json.dumps(prompt_result, default=str)}],
            "isError": isinstance(result, dict) and "error" in result,
        }

    return sdk.tool(name, spec["description"], spec["input_schema"])(handler)


async def _query(prompt: str, options: sdk.ClaudeAgentOptions) -> sdk.ResultMessage:
    result = None
    async with asyncio.timeout(settings.claude_code_timeout_seconds):
        async for message in sdk.query(prompt=prompt, options=options):
            if isinstance(message, (sdk.AssistantMessage, sdk.UserMessage)):
                archive.event(
                    "sdk.message",
                    {"message_type": type(message).__name__, "message": asdict(message)},
                )
            if isinstance(message, sdk.SystemMessage):
                archive.event("sdk.system", {"subtype": message.subtype, "data": message.data})
            if isinstance(message, sdk.ResultMessage):
                result = message
    if result is None:
        raise ClaudeCodeError("Claude Code ended without a result")
    return result


def _usage(result: sdk.ResultMessage) -> tuple[int, int]:
    if result.model_usage:
        return (
            sum(
                usage.get("inputTokens", 0)
                + usage.get("cacheReadInputTokens", 0)
                + usage.get("cacheCreationInputTokens", 0)
                for usage in result.model_usage.values()
            ),
            sum(usage.get("outputTokens", 0) for usage in result.model_usage.values()),
        )
    usage = result.usage or {}
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        usage.get("output_tokens", 0),
    )


def run(
    *,
    purpose: str,
    model: str,
    effort: str,
    system: str,
    prompt: str,
    max_tokens: int,
    max_turns: int = 3,
    schema: dict | None = None,
    tools: list[dict] | None = None,
    execute: Callable | None = None,
) -> sdk.ResultMessage:
    if settings.model_backend != "claude_code":
        raise ClaudeCodeError("Claude Code subscription backend is disabled")
    run_id = archive.current_run.get()
    if run_id is None:
        with archive.execution("model.manual", {"purpose": purpose}):
            return run(
                purpose=purpose, model=model, effort=effort, system=system, prompt=prompt,
                max_tokens=max_tokens, max_turns=max_turns, schema=schema, tools=tools,
                execute=execute,
            )

    output_format = {"type": "json_schema", "schema": schema} if schema else None
    request_event = archive.event(
        "model.request",
        {
            "backend": "claude_code",
            "purpose": purpose,
            "request": {
                "model": model, "effort": effort, "system": system, "prompt": prompt,
                "max_tokens": max_tokens, "max_turns": max_turns,
                "output_format": output_format, "tools": tools,
            },
        },
    )
    try:
        with _process_lock, TemporaryDirectory(prefix="coach-claude-") as cwd:
            env = _session_env(Path(cwd))
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
            _verify_subscription(env, Path(cwd))
            mcp_servers = {}
            allowed_tools = []
            if tools and execute:
                mcp_servers["coach"] = sdk.create_sdk_mcp_server(
                    name="coach", tools=[_sdk_tool(spec, execute, run_id) for spec in tools]
                )
                allowed_tools = [f"mcp__coach__{spec['name']}" for spec in tools]
            web_tools = ["WebSearch", "WebFetch"] if purpose == "chat" else []
            options = sdk.ClaudeAgentOptions(
                cli_path=_launcher,
                cwd=cwd,
                env=env,
                model=model,
                system_prompt=system,
                effort=effort,
                max_turns=max_turns,
                tools=web_tools,
                allowed_tools=allowed_tools + web_tools,
                permission_mode="dontAsk",
                mcp_servers=mcp_servers,
                strict_mcp_config=True,
                setting_sources=[],
                skills=[],
                extra_args={"no-session-persistence": None, "disable-slash-commands": None},
                output_format=output_format,
            )
            result = asyncio.run(_query(prompt, options))
    except Exception as exc:
        archive.event(
            "model.error",
            {
                "request_event_id": request_event, "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        if isinstance(exc, ClaudeCodeError):
            raise
        raise ClaudeCodeError("Claude Code session failed") from None

    input_tokens, output_tokens = _usage(result)
    archive.event(
        "model.response",
        {
            "backend": "claude_code",
            "purpose": purpose,
            "request_event_id": request_event,
            "response": {
                "model": model, "session_id": result.session_id, "turns": result.num_turns,
                "stop_reason": result.stop_reason, "terminal_reason": result.terminal_reason,
                "is_error": result.is_error, "subtype": result.subtype,
                "api_error_status": result.api_error_status, "errors": result.errors,
                "permission_denials": result.permission_denials,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "text": result.result, "structured_output": result.structured_output,
            },
        },
    )
    if purpose in {"chat", "context_summary"} and (input_tokens or output_tokens):
        today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
        db.record_api_usage(today, input_tokens, output_tokens)
    expected_stops = (None, "end_turn", "tool_use") if schema else (None, "end_turn")
    if (
        result.is_error or result.subtype != "success"
        or result.terminal_reason not in (None, "completed")
        or result.stop_reason not in expected_stops
        or (schema and result.structured_output is None)
        or (not schema and not (result.result or "").strip())
    ):
        raise ClaudeCodeError("Claude Code did not complete the requested response")
    return result
