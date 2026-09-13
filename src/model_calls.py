"""Trace the exact request and provider response for every model invocation."""

from src import archive


def create(client, *, purpose: str, **kwargs):
    if archive.current_run.get() is None:
        with archive.execution("model.manual", {"purpose": purpose}):
            return create(client, purpose=purpose, **kwargs)
    request_event = archive.event("model.request", {"purpose": purpose, "request": kwargs})
    try:
        response = client.messages.create(**kwargs)
    except Exception as exc:
        archive.event(
            "model.error",
            {
                "request_event_id": request_event,
                "error_type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
            },
        )
        raise
    archive.event(
        "model.response",
        {
            "request_event_id": request_event,
            "purpose": purpose,
            "provider_request_id": getattr(response, "_request_id", None),
            "response": response.model_dump(mode="json"),
        },
    )
    return response
