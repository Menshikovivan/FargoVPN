"""Telegram media detection, bounded downloading and cache helpers."""
from __future__ import annotations

import mimetypes
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable

import httpx


SAFE_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-msvideo",
        "video/mpeg",
    }
)


def detect_media_type(content: bytes, filename: str = "", header: str = "") -> str | None:
    """Detect browser-playable image/video types, even for octet-stream responses."""
    head = bytes(content[:128])
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"

    if len(head) >= 12 and head[4:8] == b"ftyp":
        major_brand = head[8:12]
        return "video/quicktime" if major_brand == b"qt  " else "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if head.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"

    # Never trust a filename or Content-Type header when the payload clearly
    # starts as active text. This also blocks SVG/HTML files renamed to .jpg.
    textual = head.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20").lower()
    if textual.startswith((b"<svg", b"<?xml", b"<!doctype", b"<html", b"<script")):
        return None

    clean_header = str(header or "").split(";", 1)[0].strip().lower()
    if clean_header in SAFE_MEDIA_TYPES:
        return clean_header
    guessed, _ = mimetypes.guess_type(str(filename or ""))
    return guessed if guessed in SAFE_MEDIA_TYPES else None


def detect_image_media_type(content: bytes, filename: str = "", header: str = "") -> str | None:
    """Backward-compatible image-only detector used by receipt OCR."""
    media_type = detect_media_type(content, filename, header)
    return media_type if media_type and media_type.startswith("image/") else None


def media_kind(media_type: str | None) -> str | None:
    clean = str(media_type or "").split(";", 1)[0].strip().lower()
    if clean.startswith("image/"):
        return "photo"
    if clean.startswith("video/"):
        return "video"
    return None


def extension_for_media_type(media_type: str | None, fallback: str = "") -> str:
    extension = mimetypes.guess_extension(str(media_type or "").split(";", 1)[0].strip().lower())
    if extension == ".jpe":
        extension = ".jpg"
    if extension:
        return extension
    fallback_extension = Path(str(fallback or "")).suffix.lower()
    return fallback_extension if re.fullmatch(r"\.[a-z0-9]{1,8}", fallback_extension) else ".bin"


def safe_media_filename(filename: str, media_type: str | None = None) -> str:
    raw = PurePosixPath(str(filename or "media").replace("\\", "/")).name
    clean = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._ -]+", "_", raw).strip(" .")[:120]
    if not clean:
        clean = "media"
    if not Path(clean).suffix:
        clean += extension_for_media_type(media_type)
    return clean


def _telegram_file_info(client: httpx.Client, bot_token: str, file_id: str) -> tuple[str, int]:
    response = client.get(
        f"https://api.telegram.org/bot{bot_token}/getFile",
        params={"file_id": file_id},
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("Telegram вернул некорректный ответ") from error
    if response.status_code != 200 or not payload.get("ok"):
        raise RuntimeError(str(payload.get("description") or f"Telegram HTTP {response.status_code}"))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    remote_path = str(result.get("file_path") or "")
    if not remote_path:
        raise RuntimeError("Telegram не сообщил путь к файлу")
    try:
        file_size = max(0, int(result.get("file_size") or 0))
    except (TypeError, ValueError):
        file_size = 0
    return remote_path, file_size


def download_telegram_media_to_path(
    bot_token: str,
    file_id: str,
    destination: str | Path,
    *,
    timeout: float = 120.0,
    max_bytes: int = 100 * 1024 * 1024,
    allowed_kinds: Iterable[str] = ("photo", "video"),
) -> tuple[Path, str, str]:
    """Download a Telegram file atomically without keeping a large video in RAM."""
    token = str(bot_token or "").strip()
    identifier = str(file_id or "").strip()
    if not token or not identifier:
        raise RuntimeError("Не указан Telegram-токен или file_id")
    maximum = max(1, int(max_bytes))
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.{os.getpid()}.",
        suffix=".part",
        dir=str(target.parent),
    )
    os.close(file_descriptor)
    temp = Path(temp_name)
    allowed = {str(item) for item in allowed_kinds}
    client_timeout = httpx.Timeout(float(timeout), connect=min(30.0, float(timeout)))

    try:
        with httpx.Client(timeout=client_timeout, follow_redirects=True) as client:
            remote_path, reported_size = _telegram_file_info(client, token, identifier)
            if reported_size and reported_size > maximum:
                raise RuntimeError(
                    f"Файл Telegram слишком большой: {reported_size} байт, лимит {maximum}"
                )
            url = f"https://api.telegram.org/file/bot{token}/{remote_path}"
            head = bytearray()
            total = 0
            response_header = ""
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Не удалось скачать файл Telegram: HTTP {response.status_code}"
                    )
                response_header = response.headers.get("content-type", "")
                try:
                    content_length = int(response.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    content_length = 0
                if content_length and content_length > maximum:
                    raise RuntimeError(
                        f"Файл Telegram слишком большой: {content_length} байт, лимит {maximum}"
                    )
                with temp.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > maximum:
                            raise RuntimeError(f"Файл Telegram превышает лимит {maximum} байт")
                        if len(head) < 128:
                            head.extend(chunk[: 128 - len(head)])
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if total <= 0:
                raise RuntimeError("Telegram вернул пустой файл")
            media_type = detect_media_type(bytes(head), remote_path, response_header)
            kind = media_kind(media_type)
            if not media_type or kind not in allowed:
                raise RuntimeError("Файл не распознан как поддерживаемое фото или видео")
            temp.replace(target)
            os.chmod(target, 0o600)
            filename = safe_media_filename(PurePosixPath(remote_path).name, media_type)
            return target, media_type, filename
    except Exception as error:
        temp.unlink(missing_ok=True)
        # httpx exceptions may include the request URL. Never let the Bot Token
        # escape into a web error response or journal entry.
        message = str(error).replace(token, "<telegram-token-redacted>")
        if isinstance(error, RuntimeError) and message == str(error):
            raise
        raise RuntimeError(message or "Ошибка загрузки файла Telegram") from error


def download_telegram_media(
    bot_token: str,
    file_id: str,
    timeout: float = 120.0,
    *,
    max_bytes: int = 100 * 1024 * 1024,
    allowed_kinds: Iterable[str] = ("photo", "video"),
) -> tuple[bytes, str, str]:
    """Download bounded media into memory; intended for small images and tests."""
    with tempfile.TemporaryDirectory(prefix="vpn_telegram_media_") as directory:
        path, media_type, filename = download_telegram_media_to_path(
            bot_token,
            file_id,
            Path(directory) / "media.bin",
            timeout=timeout,
            max_bytes=max_bytes,
            allowed_kinds=allowed_kinds,
        )
        return path.read_bytes(), media_type, filename


def download_telegram_file(
    bot_token: str,
    file_id: str,
    timeout: float = 30.0,
) -> tuple[bytes, str, str]:
    """Download an image for receipt OCR (legacy public interface)."""
    content, media_type, filename = download_telegram_media(
        bot_token,
        file_id,
        timeout,
        max_bytes=25 * 1024 * 1024,
        allowed_kinds=("photo",),
    )
    extension = extension_for_media_type(media_type, filename)
    return content, media_type, f"telegram_file{extension}"
