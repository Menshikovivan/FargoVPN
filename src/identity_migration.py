#!/usr/bin/env python3
"""Safe recovery of Telegram IDs and usernames from legacy VPN bot databases.

The importer never guesses a Telegram ID from a username.  It matches legacy
rows to the current database by exact UUID and/or exact 3x-ui email, reports
conflicts, and only then rekeys the local row.  A successful import can also
write the recovered ``tgId`` back to 3x-ui.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import config
import user_events

MAX_SOURCE_SIZE = 512 * 1024 * 1024
MAX_DB_SIZE = 128 * 1024 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")


class IdentityImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyIdentity:
    tg_id: int
    username: str
    email: str
    uuid: str
    source_db: str


@dataclass(frozen=True)
class IdentityMatch:
    source: LegacyIdentity
    target_tg_id: int | None
    target_username: str
    target_email: str
    target_uuid: str
    reason: str
    status: str
    detail: str = ""


def import_root() -> Path:
    configured = str(getattr(config, "IDENTITY_IMPORT_DIR", "")).strip()
    if configured:
        return Path(configured)
    update_root = Path(getattr(config, "UPDATE_DIR", "/var/lib/vpn-service/updates"))
    return update_root / "identity-imports"


def _db_path(db_path: str | Path | None = None) -> str:
    return str(db_path or config.DB_PATH)


@contextmanager
def _connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(_db_path(db_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _safe_archive_name(name: str) -> str:
    normalized = str(name or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or pure.is_absolute()
        or ".." in pure.parts
        or any(part in ("", ".") for part in pure.parts)
    ):
        raise IdentityImportError(f"Небезопасный путь в архиве: {name}")
    return normalized


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}


def _candidate_column(columns: set[str], names: tuple[str, ...]) -> str | None:
    lowered = {name.lower(): name for name in columns}
    for candidate in names:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _clean_username(value: Any) -> str:
    text = str(value or "").strip().lstrip("@")
    text = re.sub(r"[\x00-\x1f\x7f]+", "", text)
    return text[:100]


def _clean_identity_text(value: Any) -> str:
    return str(value or "").strip()[:300]


def _valid_tg_id(value: Any) -> int:
    try:
        tg_id = int(value)
    except (TypeError, ValueError):
        return 0
    return tg_id if 0 < tg_id < 9_223_372_036_854_775_807 else 0


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _extract_database_candidates(source: Path, destination: Path) -> list[Path]:
    if not source.is_file():
        raise IdentityImportError("Файл импорта не найден")
    if source.stat().st_size > MAX_SOURCE_SIZE:
        raise IdentityImportError("Файл импорта слишком большой")
    destination.mkdir(parents=True, exist_ok=True)

    if _is_sqlite(source):
        target = destination / "legacy.db"
        shutil.copy2(source, target)
        os.chmod(target, 0o600)
        return [target]

    try:
        archive = tarfile.open(source, "r:*")
    except (tarfile.TarError, OSError) as error:
        raise IdentityImportError("Ожидалась SQLite-база или tar.gz-архив") from error

    candidates: list[Path] = []
    total = 0
    with archive:
        for member in archive.getmembers():
            name = _safe_archive_name(member.name)
            # Full legacy backups often contain virtualenv/system symlinks.
            # They are irrelevant to identity recovery and are never extracted;
            # rejecting the whole backup would make a valid embedded DB unusable.
            if member.issym() or member.islnk() or member.isdev():
                continue
            if not member.isfile():
                continue
            suffix = name.lower()
            if not suffix.endswith((".db", ".sqlite", ".sqlite3")):
                continue
            size = max(0, int(member.size or 0))
            if size > MAX_DB_SIZE:
                continue
            total += size
            if total > MAX_SOURCE_SIZE:
                raise IdentityImportError("Суммарный размер баз в архиве слишком большой")
            source_handle = archive.extractfile(member)
            if source_handle is None:
                continue
            digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:12]
            target = destination / f"{Path(name).stem[:60]}-{digest}.db"
            with source_handle, target.open("wb") as output:
                shutil.copyfileobj(source_handle, output)
            os.chmod(target, 0o600)
            if _is_sqlite(target):
                candidates.append(target)
            else:
                target.unlink(missing_ok=True)
    if not candidates:
        raise IdentityImportError("В архиве не найдены подходящие SQLite-базы")
    return candidates


def read_legacy_identities(path: Path) -> list[LegacyIdentity]:
    if not _is_sqlite(path):
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        table_row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)='users'"
        ).fetchone()
        if not table_row:
            return []
        table = str(table_row[0])
        columns = _table_columns(connection, table)
        tg_col = _candidate_column(columns, ("tg_id", "telegram_id", "telegramId", "user_id"))
        username_col = _candidate_column(columns, ("username", "telegram_username", "tg_username", "user_name"))
        email_col = _candidate_column(columns, ("email", "client_email", "remark"))
        uuid_col = _candidate_column(columns, ("uuid", "vless_uuid", "client_uuid", "id"))
        if not tg_col or (not email_col and not uuid_col):
            return []
        selected = [tg_col]
        for value in (username_col, email_col, uuid_col):
            if value and value not in selected:
                selected.append(value)
        query = "SELECT " + ",".join(_quote_identifier(value) for value in selected)
        query += " FROM " + _quote_identifier(table)
        rows = connection.execute(query).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    best_by_tg: dict[int, LegacyIdentity] = {}
    for row in rows:
        values = dict(row)
        tg_id = _valid_tg_id(values.get(tg_col))
        if not tg_id:
            continue
        username = _clean_username(values.get(username_col)) if username_col else ""
        email = _clean_identity_text(values.get(email_col)) if email_col else ""
        uuid_value = _clean_identity_text(values.get(uuid_col)) if uuid_col else ""
        if not email and not uuid_value:
            continue
        item = LegacyIdentity(tg_id, username, email, uuid_value, path.name)
        score = int(bool(username)) + int(bool(email)) * 2 + int(bool(uuid_value)) * 3
        previous = best_by_tg.get(tg_id)
        if previous:
            previous_score = int(bool(previous.username)) + int(bool(previous.email)) * 2 + int(bool(previous.uuid)) * 3
            if previous_score >= score:
                continue
        best_by_tg[tg_id] = item
    return sorted(best_by_tg.values(), key=lambda item: item.tg_id)


def select_best_database(source: Path) -> tuple[Path, list[LegacyIdentity], list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="vpn_identity_scan_") as directory:
        candidates = _extract_database_candidates(source, Path(directory))
        reports: list[dict[str, Any]] = []
        selected_path: Path | None = None
        selected_rows: list[LegacyIdentity] = []
        for candidate in candidates:
            rows = read_legacy_identities(candidate)
            complete = sum(1 for row in rows if row.username and row.email and row.uuid)
            score = len(rows) * 10 + complete * 5
            reports.append(
                {
                    "database": candidate.name,
                    "rows": len(rows),
                    "complete": complete,
                    "score": score,
                }
            )
            if selected_path is None or score > (len(selected_rows) * 10 + sum(1 for row in selected_rows if row.username and row.email and row.uuid) * 5):
                selected_path = candidate
                selected_rows = rows
        if selected_path is None or not selected_rows:
            raise IdentityImportError("В найденных базах нет пригодных Telegram-привязок")
        retained_fd, retained_name = tempfile.mkstemp(prefix="vpn_identity_selected_", suffix=".db")
        os.close(retained_fd)
        retained = Path(retained_name)
        shutil.copy2(selected_path, retained)
        os.chmod(retained, 0o600)
    return retained, selected_rows, reports


def stage_import(source: Path, original_name: str = "legacy.db") -> dict[str, Any]:
    root = import_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    prune_staged_imports()
    selected, rows, candidates = select_best_database(source)
    token = secrets.token_urlsafe(24)
    target = root / f"{token}.db"
    metadata_path = root / f"{token}.json"
    try:
        shutil.copy2(selected, target)
        os.chmod(target, 0o600)
        metadata = {
            "token": token,
            "original_name": Path(original_name).name[:200],
            "created_at": int(time.time()),
            "selected_rows": len(rows),
            "candidates": candidates,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
        temporary = metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(metadata_path)
        return metadata
    finally:
        selected.unlink(missing_ok=True)


def _staged_paths(token: str) -> tuple[Path, Path]:
    if not TOKEN_RE.fullmatch(str(token or "")):
        raise IdentityImportError("Некорректный токен импорта")
    root = import_root().resolve()
    db = (root / f"{token}.db").resolve()
    metadata = (root / f"{token}.json").resolve()
    if root not in db.parents or root not in metadata.parents:
        raise IdentityImportError("Некорректный путь импорта")
    if not db.is_file() or not metadata.is_file():
        raise IdentityImportError("Подготовленный импорт не найден или уже удалён")
    return db, metadata


def staged_metadata(token: str) -> dict[str, Any]:
    _, metadata = _staged_paths(token)
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise IdentityImportError("Метаданные импорта повреждены") from error
    return data if isinstance(data, dict) else {}


def prune_staged_imports(max_age_seconds: int = 86_400) -> int:
    root = import_root()
    if not root.exists():
        return 0
    cutoff = time.time() - max(300, int(max_age_seconds))
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _current_users(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with _connect(db_path) as connection:
        columns = _table_columns(connection, "users")
        wanted = [name for name in ("tg_id", "username", "email", "uuid") if name in columns]
        rows = connection.execute(
            "SELECT " + ",".join(_quote_identifier(name) for name in wanted) + " FROM users"
        ).fetchall()
    return [dict(row) for row in rows]


def build_preview(
    source_rows: list[LegacyIdentity],
    db_path: str | Path | None = None,
) -> list[IdentityMatch]:
    current = _current_users(db_path)

    def index_by(field: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in current:
            value = str(row.get(field) or "").strip().lower()
            if value:
                result.setdefault(value, []).append(row)
        return result

    by_uuid = index_by("uuid")
    by_email = index_by("email")
    by_tg = {int(row.get("tg_id") or 0): row for row in current}
    source_tg_counts: dict[int, int] = {}
    for row in source_rows:
        source_tg_counts[row.tg_id] = source_tg_counts.get(row.tg_id, 0) + 1

    preview: list[IdentityMatch] = []
    for source in source_rows:
        uuid_matches = by_uuid.get(source.uuid.lower(), []) if source.uuid else []
        email_matches = by_email.get(source.email.lower(), []) if source.email else []
        combined: dict[int, dict[str, Any]] = {}
        for item in uuid_matches + email_matches:
            combined[int(item.get("tg_id") or 0)] = item

        reason = ""
        target: dict[str, Any] | None = None
        if len(combined) == 1:
            target = next(iter(combined.values()))
            uuid_ok = bool(source.uuid and uuid_matches)
            email_ok = bool(source.email and email_matches)
            reason = "uuid+email" if uuid_ok and email_ok else ("uuid" if uuid_ok else "email")
        elif len(combined) > 1:
            preview.append(
                IdentityMatch(source, None, "", "", "", "uuid/email", "conflict", "UUID и email указывают на разные текущие записи")
            )
            continue
        elif source.tg_id in by_tg:
            target = by_tg[source.tg_id]
            reason = "tg_id"
        else:
            preview.append(
                IdentityMatch(source, None, "", "", "", "none", "unmatched", "Совпадение по UUID/email не найдено")
            )
            continue

        target_tg_id = int(target.get("tg_id") or 0)
        occupied = by_tg.get(source.tg_id)
        if source_tg_counts.get(source.tg_id, 0) > 1:
            status, detail = "conflict", "Один Telegram ID встречается в нескольких исходных строках"
        elif occupied and int(occupied.get("tg_id") or 0) != target_tg_id:
            same = (
                (source.uuid and str(occupied.get("uuid") or "").lower() == source.uuid.lower())
                or (source.email and str(occupied.get("email") or "").lower() == source.email.lower())
            )
            status, detail = (
                ("ready", "Telegram ID уже занят дубликатом той же записи; строки будут объединены")
                if same
                else ("conflict", "Telegram ID уже принадлежит другому пользователю")
            )
        elif target_tg_id > 0 and target_tg_id != source.tg_id:
            status, detail = "conflict", f"В текущей базе уже задан другой положительный Telegram ID ({target_tg_id})"
        elif target_tg_id == source.tg_id and str(target.get("username") or "") == source.username:
            status, detail = "already", "Привязка уже актуальна"
        else:
            status, detail = "ready", "Готово к восстановлению"

        preview.append(
            IdentityMatch(
                source=source,
                target_tg_id=target_tg_id,
                target_username=str(target.get("username") or ""),
                target_email=str(target.get("email") or ""),
                target_uuid=str(target.get("uuid") or ""),
                reason=reason,
                status=status,
                detail=detail,
            )
        )
    return preview


def preview_staged(token: str, db_path: str | Path | None = None) -> tuple[dict[str, Any], list[IdentityMatch]]:
    db, _ = _staged_paths(token)
    rows = read_legacy_identities(db)
    if not rows:
        raise IdentityImportError("В подготовленной базе больше нет пригодных привязок")
    return staged_metadata(token), build_preview(rows, db_path)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return _table_exists(connection, table) and column in _table_columns(connection, table)


def _rekey_referral_rows(connection: sqlite3.Connection, old_tg_id: int, new_tg_id: int) -> None:
    """Keep referral history attached when a Telegram identity changes."""
    if not _table_exists(connection, "referral_rewards") or old_tg_id == new_tg_id:
        return
    connection.execute(
        "UPDATE referral_rewards SET referrer_tg_id=? WHERE referrer_tg_id=?",
        (int(new_tg_id), int(old_tg_id)),
    )
    connection.execute(
        "UPDATE referral_rewards SET referred_tg_id=? WHERE referred_tg_id=?",
        (int(new_tg_id), int(old_tg_id)),
    )


def _merge_duplicate_user(
    connection: sqlite3.Connection,
    source_tg_id: int,
    target_tg_id: int,
    username: str,
) -> None:
    # Move dependent history before deleting the placeholder row.  Payments and
    # events are safe to merge because both rows represent the same UUID/email.
    for table in ("payments", "message_log", "user_events"):
        if _column_exists(connection, table, "tg_id"):
            connection.execute(
                f"UPDATE {_quote_identifier(table)} SET tg_id=? WHERE tg_id=?",
                (int(source_tg_id), int(target_tg_id)),
            )
    user_events.rekey_message_state(connection, int(target_tg_id), int(source_tg_id))
    _rekey_referral_rows(connection, int(target_tg_id), int(source_tg_id))
    connection.execute("DELETE FROM users WHERE tg_id=?", (int(target_tg_id),))
    if username:
        connection.execute("UPDATE users SET username=? WHERE tg_id=?", (username, int(source_tg_id)))


def rebind_local_identity(
    old_tg_id: int,
    new_tg_id: int,
    username: str | None,
    *,
    source: str = "manual",
    overwrite_positive: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    old_tg_id, new_tg_id = int(old_tg_id), _valid_tg_id(new_tg_id)
    if not new_tg_id:
        raise IdentityImportError("Telegram ID должен быть положительным числом")
    clean_username = _clean_username(username)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM users WHERE tg_id=?", (old_tg_id,)).fetchone()
        if not row:
            raise IdentityImportError("Текущий пользователь не найден")
        current = dict(row)
        if old_tg_id > 0 and old_tg_id != new_tg_id and not overwrite_positive:
            raise IdentityImportError("Положительный Telegram ID можно заменить только в режиме подтверждённого конфликта")
        occupied_row = connection.execute("SELECT * FROM users WHERE tg_id=?", (new_tg_id,)).fetchone()
        if occupied_row and new_tg_id != old_tg_id:
            occupied = dict(occupied_row)
            same_identity = (
                current.get("uuid")
                and occupied.get("uuid")
                and str(current["uuid"]).lower() == str(occupied["uuid"]).lower()
            ) or (
                current.get("email")
                and occupied.get("email")
                and str(current["email"]).lower() == str(occupied["email"]).lower()
            )
            if not same_identity:
                raise IdentityImportError("Новый Telegram ID уже используется другим пользователем")
            _merge_duplicate_user(connection, new_tg_id, old_tg_id, clean_username)
            result_tg = new_tg_id
            connection.execute(
                "UPDATE users SET username=COALESCE(NULLIF(?,''),username),identity_source=?,identity_updated_at=? WHERE tg_id=?",
                (clean_username, str(source)[:200], timestamp, result_tg),
            )
        else:
            if new_tg_id != old_tg_id:
                for table in ("payments", "message_log", "user_events"):
                    if _column_exists(connection, table, "tg_id"):
                        connection.execute(
                            f"UPDATE {_quote_identifier(table)} SET tg_id=? WHERE tg_id=?",
                            (new_tg_id, old_tg_id),
                        )
                user_events.rekey_message_state(connection, old_tg_id, new_tg_id)
                _rekey_referral_rows(connection, old_tg_id, new_tg_id)
                connection.execute("UPDATE users SET tg_id=? WHERE tg_id=?", (new_tg_id, old_tg_id))
            result_tg = new_tg_id
            connection.execute(
                "UPDATE users SET username=COALESCE(NULLIF(?,''),username),identity_source=?,identity_updated_at=? WHERE tg_id=?",
                (clean_username, str(source)[:200], timestamp, result_tg),
            )
        result = connection.execute("SELECT * FROM users WHERE tg_id=?", (result_tg,)).fetchone()
    if not result:
        raise IdentityImportError("Не удалось сохранить Telegram-привязку")
    return dict(result)



def bind_existing_panel_client(
    current_tg_id: int,
    new_tg_id: int,
    email: str,
    username: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Safely attach an existing 3x-ui client to a FargoVPN identity.

    This does not create a new 3x-ui client. The selected panel client remains the
    same UUID/subId/expiry record. The local FargoVPN row is re-bound to the new
    Telegram ID only when the destination ID is free; an existing different user's
    row or a client already attached to another local user blocks the operation.
    """
    current_tg_id = int(current_tg_id)
    new_tg_id = _valid_tg_id(new_tg_id)
    clean_email = str(email or "").strip()
    clean_username = _clean_username(username)
    if current_tg_id == 0:
        raise IdentityImportError("Текущий Telegram ID не указан")
    if not new_tg_id:
        raise IdentityImportError("Новый Telegram ID должен быть положительным числом")
    if not clean_email:
        raise IdentityImportError("Нужно указать email существующего клиента 3x-ui")

    from services.xui_api import get_client_record_sync, bind_client_tg_id_sync

    record = get_client_record_sync(clean_email)
    normalized = dict(record.get("normalized") or {})
    actual_email = str(record.get("client", {}).get("email") or clean_email).strip()
    selected_uuid = str(normalized.get("uuid") or "").strip()
    if not selected_uuid:
        raise IdentityImportError("У выбранного клиента 3x-ui нет UUID")

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM users WHERE tg_id=?", (current_tg_id,)).fetchone()
        if not current:
            raise IdentityImportError("Текущий пользователь не найден")
        occupant = connection.execute("SELECT * FROM users WHERE tg_id=?", (new_tg_id,)).fetchone()
        if occupant and new_tg_id != current_tg_id:
            raise IdentityImportError("Новый Telegram ID уже используется другим пользователем")
        email_owner = connection.execute(
            "SELECT tg_id FROM users WHERE lower(COALESCE(email,''))=lower(?) AND tg_id<>? LIMIT 1",
            (actual_email, current_tg_id),
        ).fetchone()
        uuid_owner = connection.execute(
            "SELECT tg_id FROM users WHERE lower(COALESCE(uuid,''))=lower(?) AND tg_id<>? LIMIT 1",
            (selected_uuid, current_tg_id),
        ).fetchone()
        if email_owner or uuid_owner:
            owner_id = int((email_owner or uuid_owner)[0])
            raise IdentityImportError(f"Выбранный 3x-ui клиент уже связан с пользователем Telegram ID {owner_id}")
        referral_rekey_conflict = connection.execute(
            """
            SELECT 1 FROM referral_rewards
             WHERE referred_tg_id=? AND referred_tg_id<>?
             LIMIT 1
            """,
            (new_tg_id, current_tg_id),
        ).fetchone()
        if referral_rekey_conflict:
            raise IdentityImportError("Новый Telegram ID уже участвует в другой реферальной связи")

        # Preserve the exact panel client fields as the source of truth for the
        # subscription while changing only the local identity binding.
        target_fields = {
            "email": actual_email,
            "uuid": selected_uuid,
            "expiry_time": int(normalized.get("expiry_time") or 0),
            "enable": int(bool(normalized.get("enable", True))),
            "sub_id": str(normalized.get("sub_id") or ""),
            "username": clean_username or str(current["username"] or ""),
        }

    # The XUI write occurs outside the local DB transaction. It is an idempotent
    # identity assignment; if it fails, no local data is changed.
    bind_client_tg_id_sync(actual_email, new_tg_id)

    try:
        with _connect(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM users WHERE tg_id=?", (current_tg_id,)).fetchone()
            if not current:
                raise IdentityImportError("Текущий пользователь исчез во время привязки")
            occupant = connection.execute("SELECT tg_id FROM users WHERE tg_id=?", (new_tg_id,)).fetchone()
            if occupant and new_tg_id != current_tg_id:
                raise IdentityImportError("Новый Telegram ID заняли во время операции; локальные данные не объединены")
            if new_tg_id != current_tg_id:
                for table in ("payments", "message_log", "user_events"):
                    if _column_exists(connection, table, "tg_id"):
                        connection.execute(
                            f"UPDATE {_quote_identifier(table)} SET tg_id=? WHERE tg_id=?",
                            (new_tg_id, current_tg_id),
                        )
                user_events.rekey_message_state(connection, current_tg_id, new_tg_id)
                _rekey_referral_rows(connection, current_tg_id, new_tg_id)
                connection.execute("UPDATE users SET tg_id=? WHERE tg_id=?", (new_tg_id, current_tg_id))
            connection.execute(
                """
                UPDATE users
                   SET username=COALESCE(NULLIF(?,''),username),
                       email=?,uuid=?,expiry_time=?,enable=?,sub_id=?,
                       identity_source='3x-ui:manual-bind',identity_updated_at=CURRENT_TIMESTAMP
                 WHERE tg_id=?
                """,
                (
                    target_fields["username"], target_fields["email"], target_fields["uuid"],
                    target_fields["expiry_time"], target_fields["enable"], target_fields["sub_id"], new_tg_id,
                ),
            )
            result = connection.execute("SELECT * FROM users WHERE tg_id=?", (new_tg_id,)).fetchone()
    except Exception:
        # The remote client was already updated. If the local identity transaction
        # cannot be completed, restore the original panel Telegram ID so the two
        # systems do not point at different users.
        old_panel_tg = int((record.get("client") or {}).get("tgId") or 0)
        try:
            if old_panel_tg != new_tg_id:
                from services.xui_api import update_client_sync
                update_client_sync(actual_email, {"tgId": old_panel_tg}, record=record)
        except Exception as rollback_error:
            logger.exception("Не удалось откатить Telegram ID клиента 3x-ui после локальной ошибки: %s", rollback_error)
        raise
    if not result:
        raise IdentityImportError("Не удалось сохранить привязанный клиент")
    return dict(result)

def apply_staged(
    token: str,
    *,
    db_path: str | Path | None = None,
    sync_panel: bool = True,
    overwrite_conflicts: bool = False,
) -> dict[str, Any]:
    metadata, preview = preview_staged(token, db_path)
    summary: dict[str, Any] = {
        "scanned": len(preview),
        "matched": sum(1 for item in preview if item.status in {"ready", "already", "conflict"} and item.target_tg_id is not None),
        "updated": 0,
        "already": 0,
        "unmatched": 0,
        "conflicts": 0,
        "panel_updated": 0,
        "panel_errors": [],
        "items": [],
    }

    panel_updates: list[tuple[str, int]] = []
    for item in preview:
        if item.status == "unmatched":
            summary["unmatched"] += 1
            continue
        if item.status == "already":
            summary["already"] += 1
            continue
        if item.status == "conflict" and not overwrite_conflicts:
            summary["conflicts"] += 1
            summary["items"].append({"tg_id": item.source.tg_id, "status": "conflict", "detail": item.detail})
            continue
        if item.target_tg_id is None:
            summary["unmatched"] += 1
            continue
        try:
            result = rebind_local_identity(
                item.target_tg_id,
                item.source.tg_id,
                item.source.username,
                source=f"legacy:{metadata.get('original_name', 'database')}",
                overwrite_positive=bool(overwrite_conflicts),
                db_path=db_path,
            )
            summary["updated"] += 1
            email = str(result.get("email") or item.source.email)
            if sync_panel and email:
                panel_updates.append((email, item.source.tg_id))
            summary["items"].append({"tg_id": item.source.tg_id, "status": "updated", "email": email})
        except Exception as error:
            summary["conflicts"] += 1
            summary["items"].append({"tg_id": item.source.tg_id, "status": "error", "detail": str(error)})

    if panel_updates:
        try:
            from services.xui_api import bind_client_tg_id_sync
        except Exception as error:
            summary["panel_errors"].append(str(error))
            bind_client_tg_id_sync = None  # type: ignore[assignment]
        if bind_client_tg_id_sync is not None:
            for email, tg_id in panel_updates:
                try:
                    bind_client_tg_id_sync(email, tg_id)
                    summary["panel_updated"] += 1
                except Exception as error:
                    summary["panel_errors"].append(f"{email}: {error}")

    details = json.dumps(
        {key: value for key, value in summary.items() if key != "items"},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )[:12_000]
    with _connect(db_path) as connection:
        if _table_exists(connection, "identity_import_runs"):
            connection.execute(
                """
                INSERT INTO identity_import_runs(
                    source_name,scanned,matched,updated,conflicts,panel_updated,details
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(metadata.get("original_name") or "legacy database")[:200],
                    summary["scanned"],
                    summary["matched"],
                    summary["updated"],
                    summary["conflicts"],
                    summary["panel_updated"],
                    details,
                ),
            )

    # One-time personal data is removed immediately after use.
    db, meta_path = _staged_paths(token)
    db.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return summary


def preview_summary(preview: list[IdentityMatch]) -> dict[str, int]:
    result = {"total": len(preview), "ready": 0, "already": 0, "unmatched": 0, "conflict": 0}
    for item in preview:
        result[item.status] = result.get(item.status, 0) + 1
    return result


def _masked(value: str) -> str:
    value = str(value or "")
    if len(value) <= 6:
        return value
    return value[:3] + "…" + value[-3:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover Telegram identities from a legacy VPN bot database/archive")
    parser.add_argument("--source", required=True)
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync-panel", action="store_true")
    parser.add_argument("--overwrite-conflicts", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    metadata = stage_import(Path(args.source), Path(args.source).name)
    _, preview = preview_staged(str(metadata["token"]), args.db)
    if args.apply:
        result: Any = apply_staged(
            str(metadata["token"]),
            db_path=args.db,
            sync_panel=args.sync_panel,
            overwrite_conflicts=args.overwrite_conflicts,
        )
    else:
        result = {
            "metadata": metadata,
            "summary": preview_summary(preview),
            "items": [
                {
                    "tg_id": _masked(str(item.source.tg_id)),
                    "username": _masked(item.source.username),
                    "status": item.status,
                    "reason": item.reason,
                    "detail": item.detail,
                }
                for item in preview
            ],
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(result if args.apply else result["summary"], ensure_ascii=False, indent=2, default=str))
        if not args.apply:
            print("Preview only. Re-run with --apply after reviewing the report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
