from __future__ import annotations

import ast
import html
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))

from services.telegram_events import incoming_message_event_payload  # noqa: E402


def _function_source(name: str) -> tuple[str, ast.FunctionDef]:
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return source, function


def _load_web_helpers():
    source = (APP / "webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"event_for_web", "_event_media_html"}
    ]
    namespace = {
        "Any": Any,
        "html": html,
        "re": re,
        "public_path": lambda path: "/panel" + path,
        "cabinet_service": SimpleNamespace(
            public_username=lambda tg_id, *values: next(
                (
                    str(value).strip().lstrip("@")
                    for value in values
                    if value
                    and str(value).strip().lstrip("@").casefold()
                    not in {str(tg_id), f"id_{tg_id}".casefold()}
                ),
                "",
            )
        ),
        "_panel_user_label": lambda tg_id, username, display_name, comment="": (
            f"@{username}" if username else comment or f"ID {tg_id}"
        ),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "webapp_media_helpers.py", "exec"), namespace)
    return namespace


def test_incoming_photo_event_keeps_telegram_file_metadata():
    photo = SimpleNamespace(
        file_id="telegram-file-id",
        file_unique_id="unique-photo-id",
        file_size=12345,
        width=640,
        height=480,
    )
    message = SimpleNamespace(
        photo=[photo],
        video=None,
        animation=None,
        video_note=None,
        document=None,
        sticker=None,
        content_type="photo",
        text=None,
        caption=None,
        message_id=77,
    )

    event_type, text, metadata = incoming_message_event_payload(message)

    assert event_type == "telegram_photo"
    assert text == "[Фото]"
    assert metadata["media"]["file_id"] == "telegram-file-id"
    assert metadata["media"]["kind"] == "photo"
    assert metadata["media"]["mime_type"] == "image/jpeg"


def test_event_media_url_is_prefix_aware_and_does_not_expose_file_id():
    helpers = _load_web_helpers()
    event = {
        "id": 42,
        "metadata": {
            "media": {
                "kind": "photo",
                "file_id": "secret-telegram-file-id",
                "file_name": "photo.jpg",
                "mime_type": "image/jpeg",
            }
        },
    }

    public_event = helpers["event_for_web"](event, 123)
    media_html = helpers["_event_media_html"](public_event)

    assert public_event["metadata"]["media"]["url"] == "/panel/api/users/123/events/42/media"
    assert "file_id" not in public_event["metadata"]["media"]
    assert "secret-telegram-file-id" not in media_html
    assert '<img class="chat-media-image"' in media_html
    assert 'src="/panel/api/users/123/events/42/media"' in media_html


def test_initial_messages_history_renders_media_and_authenticated_media_route():
    source, messages_page = _function_source("messages_page")
    page_source = ast.get_source_segment(source, messages_page)
    assert page_source is not None
    assert "_event_media_html(event_for_web(event, tid))" in page_source

    media_source, route = _function_source("user_event_media")
    route_source = ast.get_source_segment(media_source, route)
    assert route_source is not None
    assert route_source.index("require_auth(request)") < route_source.index("cached_event_media(")
    assert "content_disposition_type=\"inline\"" in route_source
    assert '"Cache-Control": "private, no-store"' in route_source
