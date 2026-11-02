"""Bounded views of archived tool data; original results stay in the trace."""

import json

from src import archive, db

MAX_RESULT_BYTES = 12000
READ_TOOLS = {
    "get_coaching_profile",
    "search_conversation_history",
    "read_conversation",
    "get_activity_detail",
    "get_recent_runs",
    "get_wellness_window",
    "get_last_brief",
    "get_garmin_summary",
    "search_workouts",
    "read_tool_result",
}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _path(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _description(value):
    if isinstance(value, dict):
        return {"type": "object", "entries": len(value)}
    if isinstance(value, list):
        return {"type": "array", "entries": len(value)}
    if isinstance(value, str):
        return {"type": "string", "characters": len(value)}
    return {"type": type(value).__name__}


def reference(event_id: int, name: str, value) -> dict:
    return {
        "archived_result": event_id,
        "tool": name,
        **_description(value),
        "omitted": True,
        "notice": "Data is in the trace, not lost. Call read_tool_result with this event_id "
        "and a JSON-pointer path/offset to read exact data. Do not infer facts from omitted data.",
    }


def _load(event_id: int, *, previous_sessions: bool = False):
    if type(event_id) is not int or event_id < 1:
        raise ValueError("event_id must be a positive integer")
    current = archive.get_run(archive.current_run.get())
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT e.payload,r.chat_id,r.session_id FROM run_events e "
            "JOIN agent_runs r ON r.id=e.run_id WHERE e.id=? AND e.kind='tool.finished'",
            (event_id,),
        ).fetchone()
    if (
        row is None
        or row["chat_id"] != current["chat_id"]
        or (row["session_id"] != current["session_id"] and not previous_sessions)
    ):
        raise ValueError("tool result is outside the permitted conversation scope")
    payload = json.loads(row["payload"])
    return payload["name"], payload["result"]


def _select(value, path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("path must be an empty string or JSON pointer such as /workouts/0")
    for part in path.split("/")[1:] if path else []:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isdecimal() or int(key) >= len(value):
                raise ValueError("array path index is out of range")
            value = value[int(key)]
        elif isinstance(value, dict) and key in value:
            value = value[key]
        else:
            raise ValueError("path does not exist in this result")
    return value


def page(event_id: int, name: str, root, path: str = "", offset: int = 0, limit: int = 10):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("offset must be nonnegative; limit must be 1–50")
    value = _select(root, path)
    result = {"event_id": event_id, "tool": name, "path": path, **_description(value)}
    if isinstance(value, (dict, list)):
        keys = list(value) if isinstance(value, dict) else range(len(value))
        if offset > len(keys):
            raise ValueError("offset is out of range")
        result.update(offset=offset, items=[])
        for key in keys[offset : offset + limit]:
            child = value[key]
            child_path = _path(path, key)
            entry = {"key": key, "path": child_path}
            # Oversized children remain addressable rather than cut in half.
            if _size(child) <= 2000:
                entry["value"] = child
            else:
                entry.update(omitted=True, **_description(child))
            candidate = {**result, "items": result["items"] + [entry]}
            if _size(candidate) > MAX_RESULT_BYTES - 500:
                break
            result["items"].append(entry)
        result["next_offset"] = offset + len(result["items"])
        result["complete"] = (
            result["next_offset"] == len(keys)
            and not any(e.get("omitted") for e in result["items"])
            and offset == 0
        )
        if result["next_offset"] == len(keys):
            result["next_offset"] = None
        if offset < len(keys) and not result["items"]:
            raise ValueError("field name is too large to display in a result page")
    elif isinstance(value, str):
        if offset > len(value):
            raise ValueError("offset is out of range")
        chunk = value[offset : offset + 1000]
        while _size({**result, "text": chunk}) > MAX_RESULT_BYTES - 300:
            chunk = chunk[: len(chunk) // 2]
            if not chunk:
                raise ValueError("path is too large to display")
        result.update(
            offset=offset,
            text=chunk,
            next_offset=offset + len(chunk) if offset + len(chunk) < len(value) else None,
            complete=offset == 0 and len(chunk) == len(value),
        )
    else:
        result.update(value=value, complete=True)
    result["notice"] = (
        "Exact page only. Follow next_offset or an omitted item's path for more data."
    )
    if _size(result) > MAX_RESULT_BYTES:
        raise ValueError("result path is too large to display")
    return result


def read(
    event_id: int,
    path: str = "",
    offset: int = 0,
    limit: int = 10,
    *,
    previous_sessions: bool = False,
):
    name, value = _load(event_id, previous_sessions=previous_sessions)
    return page(event_id, name, value, path, offset, limit)


def for_prompt(event_id: int, name: str, value):
    if name not in READ_TOOLS or _size(value) <= MAX_RESULT_BYTES:
        return value
    try:
        return page(event_id, name, value)
    except ValueError:
        return reference(event_id, name, value)


def compact_one(messages: list[dict]) -> tuple[list[dict], int | None]:
    """Replace the oldest useful read-result body with its durable reference.

    Match by the trusted run event, not metadata that tool content can fabricate.
    Save/update results, errors, tool IDs, and assistant evidence notes are preserved.
    """
    events = [e for e in archive.events(archive.current_run.get()) if e["kind"] == "tool.finished"]
    catalog = {e["payload"]["id"]: e for e in events if e["payload"]["name"] in READ_TOOLS}
    for i, message in enumerate(messages):
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        for j, block in enumerate(message["content"]):
            event = catalog.get(block.get("tool_use_id"))
            if block.get("type") != "tool_result" or block.get("is_error") or not event:
                continue
            payload = event["payload"]
            ref = reference(event["id"], payload["name"], payload["result"])
            if payload["name"] == "read_tool_result" and "event_id" in payload["result"]:
                page_data = payload["result"]
                ref.update(
                    archived_result=page_data["event_id"],
                    tool=page_data["tool"],
                    path=page_data["path"],
                    offset=page_data.get("offset", 0),
                )
            content = json.dumps(ref)
            if len(content) + 200 >= len(block.get("content", "")):
                continue
            blocks = list(message["content"])
            blocks[j] = {**block, "content": content}
            replay = list(messages)
            replay[i] = {**message, "content": blocks}
            return replay, event["id"]
    return messages, None
