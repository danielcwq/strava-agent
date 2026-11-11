import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import claude_agent_sdk as sdk
import pytest
from pydantic import SecretStr, ValidationError

from src import (
    archive,
    claude_code,
    context_builder,
    conversation,
    db,
    model_calls,
    model_limits,
    training_config,
)
from src.clients import claude
from src.config import Settings, settings


def result(**kwargs):
    fields = dict(
        subtype="success", duration_ms=10, duration_api_ms=9, is_error=False,
        num_turns=1, session_id="synthetic", result="Done.", stop_reason="end_turn",
        terminal_reason="completed",
        usage={"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3},
    )
    fields.update(kwargs)
    return sdk.ResultMessage(**fields)


@pytest.fixture
def subscription(monkeypatch):
    monkeypatch.setattr(settings, "model_backend", "claude_code")
    monkeypatch.setattr(
        settings, "claude_code_oauth_token", SecretStr("synthetic-subscription-token")
    )
    monkeypatch.setattr(claude_code, "_verify_subscription", lambda *args: None)
    # Tests must fail immediately if any route accidentally calls the direct API.
    def forbidden(*args, **kwargs):
        raise AssertionError("direct API must never be called")

    monkeypatch.setattr(model_calls, "create", forbidden)


def run(**kwargs):
    return claude_code.run(
        purpose="chat", model=settings.chat_model, effort="low", system="Synthetic coach",
        prompt="Synthetic request", max_tokens=4096, **kwargs,
    )


def test_launcher_removes_api_keys_and_application_credentials(tmp_path):
    probe = tmp_path / "probe"
    probe.write_text(
        "#!" + sys.executable + "\nimport os, json\nprint(json.dumps(dict(os.environ)))\n"
    )
    probe.chmod(0o700)
    extra = {
        "COACH_CLAUDE_BINARY": str(probe), "CLAUDE_CODE_OAUTH_TOKEN": "subscription",
        "ANTHROPIC_API_KEY": "api", "ANTHROPIC_AUTH_TOKEN": "bearer",
        "ANTHROPIC_BASE_URL": "https://example.invalid", "CLAUDE_CODE_USE_BEDROCK": "1",
        "TELEGRAM_BOT_TOKEN": "telegram", "GOOGLE_SERVICE_ACCOUNT_JSON_B64": "google",
        "CLAUDECODE": "1",
    }
    completed = subprocess.run(
        [str(claude_code._launcher)], env={**os.environ, **extra},
        capture_output=True, text=True, check=True,
    )
    env = json.loads(completed.stdout)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription"
    assert not (set(extra) - {"CLAUDE_CODE_OAUTH_TOKEN"}) & set(env)
    assert env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


@pytest.mark.parametrize("auth_method", ["api_key", "console", "claude.ai"])
def test_production_auth_rejects_other_billing_modes(monkeypatch, tmp_path, auth_method):
    payload = {"loggedIn": True, "apiProvider": "firstParty", "authMethod": auth_method}
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(payload))
    )
    with pytest.raises(claude_code.ClaudeCodeError, match="subscription authentication"):
        claude_code._verify_subscription({"CLAUDE_CODE_OAUTH_TOKEN": "test"}, tmp_path)


def test_explicit_token_env_is_isolated_from_local_login(subscription, tmp_path):
    env = claude_code._session_env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-subscription-token"
    assert Path(env["COACH_CLAUDE_BINARY"]).is_file()


def test_session_options_archive_and_usage(subscription, monkeypatch):
    seen = {}

    async def query(*, prompt, options):
        seen.update(prompt=prompt, options=options)
        yield sdk.AssistantMessage(content=[sdk.TextBlock("Done.")], model=settings.chat_model)
        yield result()

    monkeypatch.setattr(sdk, "query", query)
    run_id, _ = archive.enqueue("123", "chat", {"text": "Synthetic request"})
    with archive.bind(run_id):
        assert run().result == "Done."
    options = seen["options"]
    assert options.tools == ["WebSearch", "WebFetch"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == [] and options.strict_mcp_config
    assert options.fallback_model is None and options.resume is None
    assert "no-session-persistence" in options.extra_args
    assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert not Path(options.cwd).exists()
    events = archive.events(run_id)
    assert any(e["kind"] == "sdk.message" for e in events)
    response = next(e["payload"] for e in events if e["kind"] == "model.response")
    assert response["backend"] == "claude_code"
    assert response["response"]["usage"] == {"input_tokens": 15, "output_tokens": 3}
    with db.get_conn() as conn:
        assert conn.execute("SELECT sum(input_tok) FROM api_usage_daily").fetchone()[0] == 15


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_error": True, "result": "rate limited"},
        {"subtype": "error_max_turns"},
        {"stop_reason": "max_tokens"},
        {"stop_reason": "refusal"},
        {"terminal_reason": "aborted_tools"},
        {"result": ""},
    ],
)
def test_failed_or_incomplete_runs_fail_without_api_fallback(subscription, monkeypatch, kwargs):
    async def query(**unused):
        yield result(**kwargs)

    monkeypatch.setattr(sdk, "query", query)
    with pytest.raises(claude_code.ClaudeCodeError, match="did not complete"):
        run()


def test_timeout_cleans_up_sdk_iterator(subscription, monkeypatch):
    closed = []

    async def query(**kwargs):
        try:
            await asyncio.sleep(10)
            yield result()
        finally:
            closed.append(True)

    monkeypatch.setattr(settings, "claude_code_timeout_seconds", 0.01)
    monkeypatch.setattr(sdk, "query", query)
    with pytest.raises(claude_code.ClaudeCodeError, match="session failed"):
        run()
    assert closed == [True]


def test_actual_sdk_subprocess_and_mcp_bridge(subscription, monkeypatch, tmp_path):
    """Exercise the real SDK transport without a provider request or credentials."""
    fake_cli = tmp_path / "synthetic-claude"
    fake_cli.write_text(
        "#!" + sys.executable + """
import json
import sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    sys.exit()

def send(value):
    print(json.dumps(value), flush=True)

steps = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "synthetic", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "echo", "arguments": {"text": "synthetic"}}},
]

def step(index):
    send({"type": "control_request", "request_id": str(index), "request": {
        "subtype": "mcp_message", "server_name": "coach", "message": steps[index]}})

for line in sys.stdin:
    value = json.loads(line)
    if value["type"] == "control_request":
        send({"type": "control_response", "response": {
            "subtype": "success", "request_id": value["request_id"], "response": {}}})
    elif value["type"] == "user":
        step(0)
    elif value["type"] == "control_response":
        response = value["response"]
        assert response["subtype"] == "success", response
        index = int(response["request_id"])
        if index < 2:
            step(index + 1)
        else:
            payload = response["response"]["mcp_response"]
            assert json.loads(payload["result"]["content"][0]["text"]) == {
                "echo": "synthetic"}, payload
            send({"type": "result", "subtype": "success", "is_error": False,
                  "duration_ms": 1, "duration_api_ms": 0, "num_turns": 1,
                  "session_id": "synthetic", "result": "Bridge passed.",
                  "stop_reason": "end_turn", "terminal_reason": "completed"})
            break
"""
    )
    fake_cli.chmod(0o700)
    monkeypatch.setattr(
        claude_code, "_session_env",
        lambda directory: {
            "COACH_CLAUDE_BINARY": str(fake_cli), "HOME": str(directory),
            "CLAUDE_CONFIG_DIR": str(directory / "config"),
            "CLAUDE_CODE_OAUTH_TOKEN": "synthetic",
        },
    )
    tools = [{
        "name": "echo", "description": "Echo synthetic input",
        "input_schema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        },
    }]
    calls = []

    def execute(name, args):
        calls.append((name, args))
        return {"echo": args["text"]}

    assert run(tools=tools, execute=execute).result == "Bridge passed."
    assert calls == [("echo", {"text": "synthetic"})]


def test_brief_and_summary_both_route_through_subscription(subscription, monkeypatch):
    seen = []
    brief = {"headline": "Easy day", "body": "Keep it comfortable.", "flags": []}

    async def query(*, prompt, options):
        seen.append(options)
        yield result(structured_output=brief)

    monkeypatch.setattr(sdk, "query", query)
    assert claude.synthesize({"synthetic": True}) == brief
    assert conversation._summarize_history("Synthetic prior history") == "Done."
    assert seen[0].output_format["schema"] == claude.MorningBrief.model_json_schema()
    assert seen[0].tools == seen[1].tools == []
    assert seen[0].env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(settings.brief_max_tokens)


@pytest.mark.parametrize("structured_output", [None, {}, {"headline": 1}])
def test_brief_rejects_invalid_structured_output(subscription, monkeypatch, structured_output):
    async def query(**unused):
        yield result(structured_output=structured_output)

    monkeypatch.setattr(sdk, "query", query)
    with pytest.raises((claude_code.ClaudeCodeError, ValidationError)):
        claude.synthesize({})


def test_mcp_profile_tool_preserves_owner_revision_and_instruction_guards(subscription):
    instruction = "Set my next race date to 2026-12-06."
    run_id, _ = archive.enqueue("123", "chat", {"text": instruction, "sender_id": "123"})
    archive.claim_next()
    profile = training_config.get_profile()
    spec = next(t for t in conversation.TOOLS if t["name"] == "update_race_goal")
    custom = claude_code._sdk_tool(spec, conversation._execute_tool, run_id)
    args = {
        "changes": {"date": "2026-12-06"}, "expected_revision": profile["revision"],
        "instruction": instruction,
    }
    denied = asyncio.run(custom.handler({**args, "instruction": "An older message"}))
    assert denied["isError"] is True
    assert training_config.get_profile()["revision"] == profile["revision"]
    saved = asyncio.run(custom.handler(args))
    assert saved["isError"] is False
    assert training_config.get_profile()["document"]["race"]["date"] == "2026-12-06"
    assert archive.current_run.get() is None
    assert [e["kind"] for e in archive.events(run_id)].count("tool.finished") == 2


def test_chat_uses_visible_history_and_persists_final_reply(subscription, monkeypatch):
    previous_id, _ = archive.enqueue("123", "chat", {"text": "Older question"})
    with archive.bind(previous_id):
        archive.event(
            "conversation.message",
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "private"}]},
        )
        archive.event("conversation.message", {"role": "assistant", "content": "Older answer"})
    archive.finish(previous_id, "completed")
    run_id, _ = archive.enqueue("123", "chat", {"text": "New question"})

    async def query(*, prompt, options):
        request = json.loads(prompt)
        assert request["current_user_message"] == "New question"
        assert request["prior_dialogue"] == context_builder.visible_exchange(
            archive.get_run(previous_id)
        )
        assert "private" not in prompt
        assert "coach" in options.mcp_servers
        assert "mcp__coach__update_race_goal" in options.allowed_tools
        yield result()

    monkeypatch.setattr(sdk, "query", query)
    with archive.bind(run_id):
        assert conversation.handle_message("123", "New question") == "Done."
    assert archive.events(run_id)[-2]["payload"] == {"role": "assistant", "content": "Done."}


def test_other_chat_cannot_use_subscription(subscription):
    run_id, _ = archive.enqueue("999", "chat", {"text": "hi"})
    with archive.bind(run_id), pytest.raises(ValueError, match="private owner"):
        conversation.handle_message("999", "hi")


def test_sdk_context_capacity_never_looks_up_api_metadata(subscription, monkeypatch):
    monkeypatch.setattr(settings, "context_token_budget", None)
    monkeypatch.setattr(
        model_limits, "context_window",
        lambda *args: pytest.fail("subscription must not query API metadata"),
    )
    assert model_limits.input_budget() == 1_000_000 - settings.chat_max_tokens - 4096


def test_subscription_token_redacted(subscription):
    assert archive._safe({"CLAUDE_CODE_OAUTH_TOKEN": "anything"}) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "[REDACTED]"
    }
    assert archive._safe("failed: synthetic-subscription-token") == "failed: [REDACTED]"


def test_subscription_config_requires_explicit_owner_and_credentials():
    fields = dict(
        telegram_bot_token="test", telegram_chat_id="123", model_backend="claude_code",
        anthropic_api_key=None, claude_code_oauth_token=None, claude_code_use_local_login=False,
        intervals_icu_api_key="test", intervals_icu_athlete_id="test", google_sheet_id="test",
        google_service_account_json_b64="test",
    )
    with pytest.raises(ValidationError, match="setup-token"):
        Settings(_env_file=None, **fields)
    fields["claude_code_oauth_token"] = "test-token"
    assert Settings(_env_file=None, **fields).anthropic_api_key is None
    fields["telegram_chat_id"] = "-123"
    with pytest.raises(ValidationError, match="private owner"):
        Settings(_env_file=None, **fields)


def test_staging_script_passes_credentials_through_stdin_only(monkeypatch):
    from scripts import stage_claude_subscription

    calls = []
    monkeypatch.setattr(
        stage_claude_subscription.getpass, "getpass", lambda *args: "synthetic-headless-token"
    )
    monkeypatch.setattr(
        stage_claude_subscription.subprocess, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    stage_claude_subscription.main()
    args, kwargs = calls[0]
    assert args[0] == ["fly", "secrets", "import", "--stage", "-a", "strava-agent"]
    assert "synthetic-headless-token" not in " ".join(args[0])
    assert "CLAUDE_CODE_OAUTH_TOKEN=synthetic-headless-token\n" in kwargs["input"]
    assert "MODEL_BACKEND=claude_code\n" in kwargs["input"]


def test_missing_headless_token_cannot_fall_back_to_api(subscription, monkeypatch):
    monkeypatch.setattr(settings, "claude_code_oauth_token", None)
    monkeypatch.setattr(settings, "claude_code_use_local_login", False)
    with pytest.raises(claude_code.ClaudeCodeError, match="setup-token is missing"):
        run()


def test_direct_api_helper_is_disabled_in_subscription_mode(monkeypatch):
    monkeypatch.setattr(settings, "model_backend", "claude_code")
    with pytest.raises(RuntimeError, match="disabled"):
        model_calls.create(None, purpose="test")
