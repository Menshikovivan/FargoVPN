"""Pure helpers for converting Telegram messages into web-chat journal events.

This module deliberately has no aiogram imports, so media extraction can be
unit-tested without creating a bot or contacting Telegram.
"""
from __future__ import annotations

from typing import Any


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _telegram_file_metadata(
    file_object: Any,
    *,
    kind: str,
    default_mime: str = "",
    default_name: str = "",
) -> dict[str, Any] | None:
    if file_object is None:
        return None
    file_id = str(getattr(file_object, "file_id", "") or "")
    if not file_id:
        return None
    mime_type = str(getattr(file_object, "mime_type", "") or default_mime)
    file_name = str(getattr(file_object, "file_name", "") or default_name)
    result: dict[str, Any] = {
        "kind": kind,
        "file_id": file_id,
        "file_unique_id": str(getattr(file_object, "file_unique_id", "") or ""),
        "mime_type": mime_type,
        "file_name": file_name,
        "file_size": safe_int(getattr(file_object, "file_size", 0)),
        "width": safe_int(getattr(file_object, "width", 0)),
        "height": safe_int(getattr(file_object, "height", 0)),
        "duration": safe_int(getattr(file_object, "duration", 0)),
    }
    return {key: value for key, value in result.items() if value not in ("", 0, None)}


def telegram_message_media(message: Any) -> dict[str, Any] | None:
    """Return compact Telegram file metadata for supported incoming media."""
    photos = list(getattr(message, "photo", None) or [])
    if photos:
        return _telegram_file_metadata(
            photos[-1], kind="photo", default_mime="image/jpeg", default_name="photo.jpg"
        )

    video = getattr(message, "video", None)
    if video is not None:
        return _telegram_file_metadata(
            video, kind="video", default_mime="video/mp4", default_name="video.mp4"
        )

    animation = getattr(message, "animation", None)
    if animation is not None:
        return _telegram_file_metadata(
            animation, kind="video", default_mime="video/mp4", default_name="animation.mp4"
        )

    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        return _telegram_file_metadata(
            video_note, kind="video", default_mime="video/mp4", default_name="video-note.mp4"
        )

    document = getattr(message, "document", None)
    if document is not None:
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        filename = str(getattr(document, "file_name", "") or "document")
        if mime_type.startswith("image/"):
            return _telegram_file_metadata(
                document, kind="photo", default_mime=mime_type, default_name=filename
            )
        if mime_type.startswith("video/"):
            return _telegram_file_metadata(
                document, kind="video", default_mime=mime_type, default_name=filename
            )
        return _telegram_file_metadata(
            document, kind="document", default_mime=mime_type, default_name=filename
        )

    sticker = getattr(message, "sticker", None)
    if sticker is not None and not bool(getattr(sticker, "is_animated", False)):
        kind = "video" if bool(getattr(sticker, "is_video", False)) else "photo"
        mime_type = "video/webm" if kind == "video" else "image/webp"
        extension = "webm" if kind == "video" else "webp"
        return _telegram_file_metadata(
            sticker,
            kind=kind,
            default_mime=mime_type,
            default_name=f"sticker.{extension}",
        )
    return None


def normalized_content_type(message: Any) -> str:
    """Convert aiogram ContentType enums to their stable lowercase value."""
    raw = getattr(message, "content_type", "message") or "message"
    value = getattr(raw, "value", raw)
    normalized = str(value or "message").strip().lower()
    if normalized.startswith("contenttype."):
        normalized = normalized.split(".", 1)[1]
    return normalized


def incoming_message_event_payload(message: Any) -> tuple[str, str, dict[str, Any]]:
    """Build one journal row for a non-callback Telegram message."""
    media = telegram_message_media(message)
    text = str(getattr(message, "text", "") or getattr(message, "caption", "") or "").strip()
    event_type = "telegram_message"
    label = "Сообщение"
    if media:
        kind = str(media.get("kind") or "")
        if kind == "photo":
            event_type, label = "telegram_photo", "Фото"
        elif kind == "video":
            event_type, label = "telegram_video", "Видео"
        else:
            event_type, label = "telegram_document", "Документ"
    elif getattr(message, "voice", None) is not None:
        event_type, label = "telegram_voice", "Голосовое сообщение"
    elif getattr(message, "audio", None) is not None:
        event_type, label = "telegram_audio", "Аудио"
    elif getattr(message, "sticker", None) is not None:
        event_type, label = "telegram_sticker", "Стикер"
    elif not text:
        normalized = normalized_content_type(message)
        label = {
            "photo": "Фото",
            "video": "Видео",
            "animation": "Анимация",
            "video_note": "Видеосообщение",
            "document": "Документ",
            "voice": "Голосовое сообщение",
            "audio": "Аудио",
            "sticker": "Стикер",
            "contact": "Контакт",
            "location": "Геопозиция",
        }.get(normalized, normalized.replace("_", " ").capitalize())

    if not text:
        text = f"[{label}]"
    metadata: dict[str, Any] = {
        "telegram_message_id": safe_int(getattr(message, "message_id", 0)),
    }
    if media:
        metadata["media"] = media
    return event_type, text, metadata
