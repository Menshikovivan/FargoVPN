#!/usr/bin/env python3
"""Safe, transactional FargoVPN + 3x-ui backup restore manager.

The web panel and installer call this module; they never implement restore logic
independently.  A restore is staged, verified, protected by a process lock and,
when the post-restore health gate fails, rolled back to the pre-restore state.
The external Nginx/L4 proxy is deliberately outside the backup scope.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = Path("/var/lib/vpn-service")
RESTORE_STATE_PATH = SERVICE_DIR / "restore-state.json"
RESTORE_LOCK_PATH = Path("/run/vpn-service-restore.lock")
RESTORE_LOG_PATH = Path("/var/log/vpn-service-restore.log")
ARCHIVE_MAX_MEMBERS = 50_000
ARCHIVE_MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
ARCHIVE_MAX_FILE_BYTES = 512 * 1024 * 1024
SAFE_PART_RE = re.compile(r"^(?P<base>.+\.tar\.gz)\.part(?P<index>\d{1,4})$")

# These tables are the user/subscription state. Other PostgreSQL tables contain
# security, audit, push, Telegram-update and worker state and are intentionally
# left untouched by "users-only" restore.
USERS_ONLY_TABLES = ("users", "payments", "referral_rewards")
APP_HEALTH_TABLES = ("users", "payments", "user_events", "audit_log")
PROTECTED_CONFIG = {
    "DATABASE_URL", "DB_PATH", "BACKUP_DIR", "BACKUP_LOCK_PATH", "BACKUP_STATE_PATH",
    "BACKUP_LIVE_STATE_PATH", "BACKUP_FORCE_PATH", "XUI_DB_ENV_FILE", "XUI_DB_PATH",
    "WEB_SOCKET_PATH", "WEB_SOCKET_GROUP", "PUSH_VAPID_PRIVATE_KEY_PATH", "INSTALL_PROFILE",
    "UPDATE_DIR", "CHAT_MEDIA_CACHE_DIR", "BROADCAST_DIR", "SUBSCRIPTION_REFRESH_DIR",
    "RESTORE_STATE_PATH", "RESTORE_LOCK_PATH", "RESTORE_LOG_PATH",
}
PROTECTED_XUI_ENV = {
    "XUI_DB_DSN", "XUI_DB_TYPE", "XUI_DB_HOST", "XUI_DB_PORT", "XUI_DB_NAME", "XUI_DB_USER", "XUI_DB_PASSWORD",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(RESTORE_LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("vpn-service-restore")


def json_out(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _safe_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def read_restore_state() -> dict[str, Any]:
    try:
        data = json.loads(RESTORE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_restore_state(
    *,
    job_id: str,
    status: str,
    phase: str,
    progress: int,
    message: str,
    mode: str = "",
    archive: str = "",
    error: str = "",
    rollback: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    current = read_restore_state()
    payload: dict[str, Any] = {
        **current,
        "status": str(status),
        "phase": str(phase),
        "progress": max(0, min(100, int(progress))),
        "message": str(message),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if job_id:
        payload["job_id"] = str(job_id)
    if mode:
        payload["mode"] = mode
    if archive:
        payload["archive"] = archive
    if error:
        payload["error"] = error
    if rollback is not None:
        payload["rollback"] = bool(rollback)
    if extra:
        payload.update(extra)
    _safe_json_write(RESTORE_STATE_PATH, payload)


def _cfg(name: str, default: Any) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _refresh_paths_from_config() -> None:
    global RESTORE_STATE_PATH, RESTORE_LOCK_PATH, RESTORE_LOG_PATH
    RESTORE_STATE_PATH = Path(str(_cfg("RESTORE_STATE_PATH", RESTORE_STATE_PATH)))
    RESTORE_LOCK_PATH = Path(str(_cfg("RESTORE_LOCK_PATH", RESTORE_LOCK_PATH)))
    RESTORE_LOG_PATH = Path(str(_cfg("RESTORE_LOG_PATH", RESTORE_LOG_PATH)))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_regular_name(name: str) -> str:
    normalized = str(name).replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("./../"):
        raise RuntimeError(f"Небезопасный путь в архиве: {name!r}")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"Небезопасный путь в архиве: {name!r}")
    if any(":" in part for part in parts[:1]):
        raise RuntimeError(f"Небезопасный путь в архиве: {name!r}")
    return normalized


def _archive_limits() -> tuple[int, int, int]:
    return (
        max(100, int(_cfg("RESTORE_ARCHIVE_MAX_MEMBERS", ARCHIVE_MAX_MEMBERS))),
        max(1024 * 1024, int(_cfg("RESTORE_ARCHIVE_MAX_UNPACKED_MB", ARCHIVE_MAX_UNPACKED_BYTES // (1024 * 1024)))) * 1024 * 1024,
        max(1024 * 1024, int(_cfg("RESTORE_ARCHIVE_MAX_FILE_MB", ARCHIVE_MAX_FILE_BYTES // (1024 * 1024)))) * 1024 * 1024,
    )


def _tar_members(archive: Path) -> list[tarfile.TarInfo]:
    if not archive.is_file():
        raise RuntimeError(f"Архив не найден: {archive}")
    if archive.stat().st_size <= 0:
        raise RuntimeError("Архив пуст")
    max_members, max_total, max_file = _archive_limits()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"Не удалось открыть tar.gz: {exc}") from exc
    if len(members) > max_members:
        raise RuntimeError(f"Архив содержит слишком много элементов: {len(members)} > {max_members}")
    total = 0
    seen_names: set[str] = set()
    for member in members:
        name = _validate_regular_name(member.name)
        if name in seen_names:
            raise RuntimeError(f"Архив содержит дублирующийся путь: {name}")
        seen_names.add(name)
        if name != "vpn_service_backup" and not name.startswith("vpn_service_backup/"):
            raise RuntimeError(f"Архив содержит путь вне vpn_service_backup/: {name}")
        if member.isdir():
            continue
        if not member.isfile():
            raise RuntimeError(f"Архив содержит недопустимый тип файла: {name!r}")
        size = int(member.size or 0)
        if size < 0 or size > max_file:
            raise RuntimeError(f"Слишком большой файл в архиве: {name} ({size} байт)")
        total += size
        if total > max_total:
            raise RuntimeError(f"Распакованный размер архива превышает лимит {max_total} байт")
    if not any(m.name == "vpn_service_backup/manifest.json" for m in members):
        raise RuntimeError("В архиве отсутствует vpn_service_backup/manifest.json")
    if not any(m.name == "vpn_service_backup/checksums.json" for m in members):
        raise RuntimeError("В архиве отсутствует vpn_service_backup/checksums.json")
    return members


def safe_extract(archive: Path, destination: Path) -> Path:
    """Validate and extract a tar.gz without symlink/device/path traversal risks."""
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    members = _tar_members(archive)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in members:
                target = (destination / _validate_regular_name(member.name)).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"Архив выходит за каталог распаковки: {member.name!r}")
            filter_fn = getattr(tarfile, "data_filter", None)
            if filter_fn is not None:
                bundle.extractall(destination, filter=filter_fn)
            else:
                bundle.extractall(destination)
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeError(f"Безопасная распаковка не удалась: {exc}") from exc
    result = destination / "vpn_service_backup"
    if not result.is_dir():
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeError("Каталог vpn_service_backup отсутствует в архиве")
    return result


def reassemble_parts(source_dir: Path, output: Path) -> Path:
    source = source_dir.expanduser().resolve()
    if not source.is_dir():
        raise RuntimeError(f"Каталог с бэкапом не найден: {source}")
    direct = sorted(p for p in source.glob("*.tar.gz") if p.is_file())
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in source.iterdir():
        if not path.is_file():
            continue
        match = SAFE_PART_RE.match(path.name)
        if match:
            groups.setdefault(match.group("base"), []).append((int(match.group("index")), path))
    if direct and groups:
        raise RuntimeError("В каталоге одновременно лежат цельный архив и части другого архива; оставьте один комплект")
    if len(direct) == 1:
        shutil.copy2(direct[0], output)
        os.chmod(output, 0o600)
        return output
    if len(direct) > 1:
        raise RuntimeError("В каталоге найдено несколько цельных .tar.gz архивов")
    if len(groups) != 1:
        raise RuntimeError("В каталоге не найден единственный комплект .tar.gz.part001/.part002")
    base, parts = next(iter(groups.items()))
    parts = sorted(parts)
    indexes = [i for i, _ in parts]
    if indexes != list(range(1, len(parts) + 1)):
        raise RuntimeError(f"Комплект {base} неполный: найдены {indexes}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as target:
        for _, part in parts:
            with part.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, target, length=1024 * 1024)
    if output.stat().st_size <= 0:
        raise RuntimeError("Склеенный архив пуст")
    os.chmod(output, 0o600)
    _tar_members(output)
    return output


def _sqlite_integrity(path: Path, *, require_users: bool = False) -> str:
    if not path.is_file():
        return "missing"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).strip().lower()
            if integrity != "ok":
                return integrity
            if require_users:
                names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "users" not in names:
                    return "users-table-missing"
            return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"


def find_application_artifact(root: Path, manifest: dict[str, Any]) -> tuple[str, Path]:
    declared = str(manifest.get("application_dump") or "").strip()
    candidates = [root / declared] if declared else []
    candidates += [
        root / "databases/fargovpn.sql",
        root / "databases/vpn_bot.db",
        root / "databases/fargovpn.db",
        root / "bot/data/vpn_bot.db",
        root / "bot/vpn_bot.db",
        root / "data/vpn_bot.db",
        root / "vpn_bot.db",
    ]
    root_resolved = root.resolve()
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if root_resolved not in path.parents:
            raise RuntimeError(f"application_dump выходит за пределы архива: {candidate}")
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if path.suffix.lower() == ".sql":
            return "PostgreSQL", path
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and _sqlite_integrity(path, require_users=True) == "ok":
            return "SQLite", path
    raise RuntimeError("В архиве отсутствует допустимая база FargoVPN")


def find_xui_artifact(root: Path, manifest: dict[str, Any]) -> tuple[str, Path]:
    declared = str(manifest.get("xui_dump") or "").strip()
    candidates = [root / declared] if declared else []
    candidates += [root / "databases/xui.sql", root / "databases/xui.db", root / "system/etc-xui/x-ui.db", root / "etc-xui/x-ui.db"]
    root_resolved = root.resolve()
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if root_resolved not in path.parents:
            raise RuntimeError(f"xui_dump выходит за пределы архива: {candidate}")
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if path.suffix.lower() == ".sql":
            return "PostgreSQL", path
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and _sqlite_integrity(path) == "ok":
            return "SQLite", path
    raise RuntimeError("В архиве отсутствует допустимая база 3x-ui")


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} повреждён: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} имеет неверный формат")
    return data


def _payload_inventory(root: Path) -> list[str]:
    result: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in {"manifest.json", "checksums.json"}:
            continue
        result.append(rel)
    return result


def verify_archive(root: Path, *, require_xui: bool = True) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Каталог архива не найден: {root}")
    manifest = _load_json_file(root / "manifest.json", "manifest.json")
    checksums = _load_json_file(root / "checksums.json", "checksums.json")
    fmt = str(manifest.get("format") or "")
    if not fmt.startswith("FargoVPN backup"):
        raise RuntimeError(f"Неизвестный формат бэкапа: {fmt!r}")
    version = str(manifest.get("version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError(f"Некорректная версия бэкапа: {version!r}")

    expected_inventory = manifest.get("inventory")
    actual_inventory = _payload_inventory(root)
    legacy_format = int(manifest.get("format_version") or 1) < 2
    if legacy_format:
        # 4.6.13 and earlier wrote checksums only for the critical DB/key payloads.
        # Keep those backups restorable after upgrade, but still verify every checksum
        # that the legacy archive declares and never trust undeclared paths.
        declared_checksums = sorted(map(str, checksums.keys()))
        if not declared_checksums:
            raise RuntimeError("Legacy-архив не содержит ни одной контрольной суммы")
        if any(rel not in actual_inventory for rel in declared_checksums):
            raise RuntimeError("checksums.json содержит путь, которого нет в архиве")
    else:
        if not isinstance(expected_inventory, list) or sorted(map(str, expected_inventory)) != actual_inventory:
            raise RuntimeError("Инвентарь архива не совпадает с фактическим содержимым")
        if sorted(map(str, checksums.keys())) != actual_inventory:
            raise RuntimeError("checksums.json не содержит полный контрольный список содержимого")

    checksum_paths = declared_checksums if legacy_format else actual_inventory
    for rel in checksum_paths:
        expected = str(checksums.get(rel) or "")
        file_path = (root / rel).resolve()
        if root not in file_path.parents or not file_path.is_file():
            raise RuntimeError(f"Файл контрольной суммы отсутствует: {rel}")
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(file_path) != expected:
            raise RuntimeError(f"Контрольная сумма не совпадает: {rel}")

    app_kind, app_path = find_application_artifact(root, manifest)
    xui_kind, xui_path = find_xui_artifact(root, manifest)
    if require_xui and manifest.get("components", {}).get("xui_db") is not True:
        raise RuntimeError("Полный архив не содержит компонент базы 3x-ui")
    components = manifest.get("components")
    if not isinstance(components, dict) or components.get("fargovpn_source") is not True or components.get("users_db") is not True:
        raise RuntimeError("Архив не содержит обязательные компоненты FargoVPN")

    xui_config = bool(components.get("xui_config"))
    systemd = bool(components.get("systemd"))
    if require_xui and (not xui_config or not systemd):
        raise RuntimeError("Полный архив не содержит обязательные конфигурации 3x-ui/systemd")

    manifest["_application_kind"] = app_kind
    manifest["_application_artifact"] = str(app_path)
    manifest["_xui_kind"] = xui_kind
    manifest["_xui_artifact"] = str(xui_path)
    manifest["_legacy_format"] = legacy_format
    return manifest


def validate_archive_file(archive: Path, *, require_xui: bool = True) -> dict[str, Any]:
    _tar_members(archive)
    with tempfile.TemporaryDirectory(prefix="fargovpn_archive_verify_") as temp:
        root = safe_extract(archive, Path(temp))
        return verify_archive(root, require_xui=require_xui)


def prepare(source_dir: Path, work_dir: Path) -> dict[str, Any]:
    work_dir = work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    archive = work_dir / "backup.tar.gz"
    reassemble_parts(source_dir, archive)
    root = safe_extract(archive, work_dir / "extracted")
    manifest = verify_archive(root)
    return {"archive": str(archive), "root": str(root), "manifest": manifest}


def dsn_password_and_cli(dsn: str) -> tuple[str, dict[str, str]]:
    from sqlalchemy.engine import make_url
    value = str(dsn or "").strip().replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = make_url(value)
    env = os.environ.copy()
    if parsed.password is not None:
        env["PGPASSWORD"] = parsed.password
        value = parsed.set(password=None).render_as_string(hide_password=False)
    return value, env


def raw_dsn(dsn: str) -> str:
    return str(dsn or "").strip().replace("postgresql+psycopg://", "postgresql://", 1)


def _pg_identifier(value: str, label: str) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError(f"Недопустимое имя PostgreSQL для {label}: {value!r}")
    return value


def pg_parts(dsn: str) -> tuple[str, str, str | None]:
    from sqlalchemy.engine import make_url
    parsed = make_url(raw_dsn(dsn))
    if not parsed.database:
        raise RuntimeError("DSN PostgreSQL не содержит имени базы")
    user = str(parsed.username or "")
    if not user:
        raise RuntimeError("DSN PostgreSQL не содержит пользователя")
    return _pg_identifier(str(parsed.database), "базы"), _pg_identifier(user, "пользователя"), parsed.host


def run_postgres_sql(sql_text: str, database: str = "postgres") -> None:
    if not (shutil.which("runuser") and shutil.which("psql")):
        raise RuntimeError("Для безопасного переключения PostgreSQL нужны локальные runuser и psql")
    completed = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-d", _pg_identifier(database, "служебной базы"), "-c", sql_text],
        capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"PostgreSQL admin-команда завершилась ошибкой: {detail[-2500:]}")


def make_staging_db(target_dsn: str, suffix: str) -> tuple[str, str]:
    target_db, target_user, _host = pg_parts(target_dsn)
    staging = _pg_identifier(f"{target_db}_restore_{suffix}"[:60], "staging-базы")
    run_postgres_sql(f'CREATE DATABASE "{staging}" OWNER "{target_user}"')
    from sqlalchemy.engine import make_url
    return make_url(raw_dsn(target_dsn)).set(database=staging).render_as_string(hide_password=False), staging


def drop_database(name: str) -> None:
    safe = _pg_identifier(name, "базы")
    try:
        run_postgres_sql(f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = \'{safe}\' AND pid <> pg_backend_pid()')
        run_postgres_sql(f'DROP DATABASE IF EXISTS "{safe}"')
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось удалить PostgreSQL базу %s: %s", safe, exc)


def restore_dump(dump: Path, dsn: str) -> None:
    if shutil.which("psql") is None:
        raise RuntimeError("Команда psql не установлена")
    command_dsn, env = dsn_password_and_cli(dsn)
    try:
        subprocess.run(
            ["psql", "--dbname", command_dsn, "--set", "ON_ERROR_STOP=1", "--file", str(dump)],
            check=True, env=env, timeout=1800, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "неизвестная ошибка").strip()
        raise RuntimeError(f"Восстановление PostgreSQL dump не прошло: {detail[-3000:]}") from exc


def app_counts(dsn: str) -> dict[str, int]:
    import psycopg
    with psycopg.connect(raw_dsn(dsn)) as conn:
        result: dict[str, int] = {}
        for table in APP_HEALTH_TABLES:
            safe = _pg_identifier(table, "таблицы")
            if conn.execute("SELECT to_regclass(%s)", (f"public.{safe}",)).fetchone()[0]:
                result[table] = int(conn.execute(f'SELECT count(*) FROM "{safe}"').fetchone()[0])
        if "users" not in result:
            raise RuntimeError("После восстановления не найдена таблица users")
        return result


def _table_columns(conn: Any, table: str, schema: str = "public") -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
        (schema, table),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _quoted_columns(columns: list[str]) -> str:
    return ", ".join('"' + col.replace('"', '""') + '"' for col in columns)


def _sequence_names(conn: Any, table: str, columns: list[str]) -> list[tuple[str, str]]:
    result = []
    for column in columns:
        row = conn.execute("SELECT pg_get_serial_sequence(%s,%s)", (f"public.{table}", column)).fetchone()
        if row and row[0]:
            result.append((str(row[0]), column))
    return result


def _reset_table_sequences(conn: Any, table: str, columns: list[str]) -> None:
    for sequence, column in _sequence_names(conn, table, columns):
        seq_sql = sequence.replace("'", "''")
        col = '"' + column.replace('"', '""') + '"'
        # setval(..., max(id), true) makes the next INSERT safe. Empty tables are reset to 1.
        conn.execute(
            f"SELECT setval(%s::regclass, COALESCE((SELECT MAX({col}) FROM public.\"{table}\"), 0) + CASE WHEN EXISTS (SELECT 1 FROM public.\"{table}\") THEN 0 ELSE 1 END, EXISTS (SELECT 1 FROM public.\"{table}\"))",
            (seq_sql,),
        )


def _ensure_users_only_safe_constraints(conn: Any) -> None:
    rows = conn.execute(
        """SELECT
               tc.table_name AS child_table,
               ccu.table_name AS parent_table
           FROM information_schema.table_constraints tc
           JOIN information_schema.constraint_column_usage ccu
             ON ccu.constraint_name = tc.constraint_name
            AND ccu.constraint_schema = tc.constraint_schema
           WHERE tc.constraint_type='FOREIGN KEY'
             AND tc.constraint_schema='public'
             AND (tc.table_name = ANY(%s) OR ccu.table_name = ANY(%s))
           ORDER BY tc.table_name, ccu.table_name""",
        (list(USERS_ONLY_TABLES), list(USERS_ONLY_TABLES)),
    ).fetchall()
    if rows:
        pairs = ", ".join(f"{a}->{b}" for a, b in rows[:12])
        raise RuntimeError(
            "Users-only restore остановлен: выбранные таблицы связаны внешними ключами "
            f"({pairs}). Безопасное частичное восстановление в этом состоянии запрещено, "
            "чтобы не нарушить ссылочную целостность или не удалить сторонние данные."
        )

def _selected_restore_with_snapshot(target_dsn: str, staging_dsn: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Restore user/subscription tables transactionally and return post-commit counts."""
    import psycopg
    with psycopg.connect(raw_dsn(target_dsn)) as conn:
        conn.autocommit = False
        snapshot_schema = _pg_identifier(f"fargovpn_restore_snapshot_{int(time.time())}_{os.getpid()}"[:55], "схемы")
        try:
            _ensure_users_only_safe_constraints(conn)
            conn.execute(f'CREATE SCHEMA "{snapshot_schema}"')
            for table in USERS_ONLY_TABLES:
                safe = _pg_identifier(table, "таблицы")
                if not conn.execute("SELECT to_regclass(%s)", (f"public.{safe}",)).fetchone()[0]:
                    raise RuntimeError(f"Текущая БД не содержит таблицу {safe}")
                conn.execute(f'CREATE TABLE "{snapshot_schema}"."{safe}" AS TABLE public."{safe}" WITH NO DATA')
                conn.execute(f'INSERT INTO "{snapshot_schema}"."{safe}" SELECT * FROM public."{safe}"')

            counts: dict[str, int] = {}
            for table in USERS_ONLY_TABLES:
                safe = _pg_identifier(table, "таблицы")
                target_columns = _table_columns(conn, safe)
                with psycopg.connect(raw_dsn(staging_dsn)) as source_conn:
                    source_columns = _table_columns(source_conn, safe)
                if not source_columns:
                    raise RuntimeError(f"Архивная БД не содержит таблицу {safe}")
                common = [column for column in target_columns if column in source_columns]
                if not common:
                    raise RuntimeError(f"Нет общих колонок для {safe}")
                cols = _quoted_columns(common)
                conn.execute(f'DELETE FROM public."{safe}"')
                # Pull only the named table with a server-side postgres_fdw is intentionally
                # avoided. Instead use a temporary table populated via COPY from psycopg in the
                # same process/database connection below.
                with psycopg.connect(raw_dsn(staging_dsn)) as source_conn:
                    with source_conn.cursor() as source_cur, conn.cursor() as target_cur:
                        source_cur.execute(f'SELECT {cols} FROM public."{safe}"')
                        placeholders = ",".join(["%s"] * len(common))
                        while True:
                            rows = source_cur.fetchmany(1000)
                            if not rows:
                                break
                            target_cur.executemany(
                                f'INSERT INTO public."{safe}" ({cols}) VALUES ({placeholders})',
                                rows,
                            )
                counts[safe] = int(conn.execute(f'SELECT count(*) FROM public."{safe}"').fetchone()[0])
                _reset_table_sequences(conn, safe, common)

            # Validate the expected archive counts before the transaction commits.
            # Otherwise a mismatch would leave modified tables committed with no snapshot
            # handle available to the caller for rollback.
            for table, expected_value in expected.items():
                if table in counts and expected_value is not None and counts[table] != int(expected_value):
                    raise RuntimeError(f"После users-only восстановления {table}: ожидалось {expected_value}, получено {counts[table]}")
            conn.commit()
            return {"counts": counts, "snapshot_schema": snapshot_schema}
        except Exception:
            conn.rollback()
            raise
        finally:
            # A later health-check failure calls _restore_selected_snapshot before dropping it.
            pass


def _restore_selected_snapshot(target_dsn: str, snapshot_schema: str) -> None:
    import psycopg
    snapshot_schema = _pg_identifier(snapshot_schema, "схемы")
    with psycopg.connect(raw_dsn(target_dsn)) as conn:
        conn.autocommit = False
        try:
            for table in USERS_ONLY_TABLES:
                safe = _pg_identifier(table, "таблицы")
                columns = _table_columns(conn, safe)
                cols = _quoted_columns(columns)
                conn.execute(f'DELETE FROM public."{safe}"')
                conn.execute(f'INSERT INTO public."{safe}" ({cols}) SELECT {cols} FROM "{snapshot_schema}"."{safe}"')
                _reset_table_sequences(conn, safe, columns)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                conn.execute(f'DROP SCHEMA IF EXISTS "{snapshot_schema}" CASCADE')
                conn.commit()
            except Exception:
                conn.rollback()


def _drop_snapshot_schema(target_dsn: str, snapshot_schema: str) -> None:
    try:
        run_postgres_sql(f'DROP SCHEMA IF EXISTS "{_pg_identifier(snapshot_schema, "схемы")}" CASCADE', database=pg_parts(target_dsn)[0])
    except Exception as exc:
        logger.warning("Не удалось удалить snapshot schema %s: %s", snapshot_schema, exc)


def restore_application_to_staging(root: Path, manifest: dict[str, Any], staging_dsn: str) -> None:
    kind, artifact = find_application_artifact(root, manifest)
    if kind == "PostgreSQL":
        restore_dump(artifact, staging_dsn)
        return
    work = Path(tempfile.mkdtemp(prefix="fargovpn_restore_sqlite_"))
    try:
        command = [
            sys.executable, str(APP_DIR / "migration_tool.py"),
            "--sqlite", str(artifact), "--dsn", staging_dsn,
            "--manifest", str(work / "migration_manifest.json"),
            "--report", str(work / "migration_report.json"),
            "--failure-report", str(work / "migration_failure.json"),
            "--reset-target",
        ]
        completed = subprocess.run(command, cwd=str(work), capture_output=True, text=True, timeout=1800)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "неизвестная ошибка").strip()
            raise RuntimeError(f"Legacy SQLite → PostgreSQL восстановление не прошло: {detail[-3000:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parse_literal_config(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = value
    return result


def merge_config(target: Path, source: Path) -> None:
    source_values = parse_literal_config(source)
    text = target.read_text(encoding="utf-8") if target.is_file() else "import aiohttp\n"
    for name, value in source_values.items():
        if name in PROTECTED_CONFIG or name.upper().startswith("NGINX_"):
            continue
        line = f"{name} = {value!r}"
        pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
        text = re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"
    compile(text, str(target), "exec")
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def merge_xui_env(target: Path, source: Path) -> None:
    if not source.is_file():
        return
    source_values = parse_env(source)
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    for key, value in source_values.items():
        if key in PROTECTED_XUI_ENV:
            continue
        rendered = f'{key}="{value.replace(chr(34), chr(92) + chr(34))}"' if any(c in value for c in " #\t") else f"{key}={value}"
        pattern = rf"(?m)^(?:export\s+)?{re.escape(key)}\s*=.*$"
        current = re.sub(pattern, rendered, current) if re.search(pattern, current) else current.rstrip() + "\n" + rendered + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(current.rstrip() + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def _service_exists(unit: str) -> bool:
    return subprocess.run(["systemctl", "cat", unit], capture_output=True, text=True, timeout=30).returncode == 0


def _external_units() -> list[str]:
    return [u for u in ("x-ui.service", "3x-ui.service", "xray.service") if _service_exists(u)]


def _fargovpn_units() -> list[str]:
    return [u for u in ("vpn-service-bot.service", "vpn-service-web.socket", "vpn-service-web.service") if _service_exists(u)]


def _capture_service_states(units: list[str]) -> dict[str, dict[str, bool]]:
    states: dict[str, dict[str, bool]] = {}
    for unit in units:
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0
        enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", unit], check=False).returncode == 0
        states[unit] = {"active": active, "enabled": enabled}
    return states


def stop_services(include_xui: bool) -> None:
    units = _fargovpn_units()
    if include_xui:
        units = _external_units() + units
    for unit in units:
        subprocess.run(["systemctl", "stop", unit], check=False, capture_output=True, text=True, timeout=60)


def start_services(full: bool) -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True, text=True, timeout=60)
    units = _external_units() if full else []
    if full:
        units += [u for u in ("vpn-service-web.socket", "vpn-service-web.service") if _service_exists(u)]
    if _service_exists("vpn-service-bot.service"):
        units.append("vpn-service-bot.service")
    for unit in units:
        result = subprocess.run(["systemctl", "start", unit], check=False, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Не удалось запустить {unit}: {detail[-1500:]}")


def _verify_unit_active(unit: str) -> None:
    if not _service_exists(unit):
        raise RuntimeError(f"Служба {unit} не установлена")
    if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode != 0:
        detail = subprocess.run(["systemctl", "status", unit, "--no-pager", "-n", "30"], check=False, capture_output=True, text=True, timeout=30)
        raise RuntimeError(f"{unit} не active после восстановления:\n{(detail.stdout or detail.stderr)[-4000:]}")


def verify_live(full: bool, config_path: Path | None = None) -> None:
    config_path = config_path or (APP_DIR / "config.py")
    values = parse_literal_config(config_path) if config_path.is_file() else {}
    _verify_unit_active("vpn-service-bot.service")
    if not full:
        return
    _verify_unit_active("vpn-service-web.socket")
    _verify_unit_active("vpn-service-web.service")
    socket_path = Path(str(values.get("WEB_SOCKET_PATH", "/run/vpn-service/fargovpn.sock")))
    if not socket_path.exists():
        raise RuntimeError(f"Unix socket веб-панели отсутствует: {socket_path}")
    if not shutil.which("curl"):
        raise RuntimeError("curl не установлен; health-check веб-панели невозможен")
    for health_path in ("/healthz", "/health"):
        completed = subprocess.run(
            ["curl", "--unix-socket", str(socket_path), "-fsS", "--max-time", "5", f"http://localhost{health_path}"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if completed.returncode == 0 and completed.stdout.strip().startswith("OK"):
            break
    else:
        raise RuntimeError("Health-check веб-панели после восстановления не пройден")
    panel_units = [u for u in ("x-ui.service", "3x-ui.service") if _service_exists(u)]
    if panel_units and not any(subprocess.run(["systemctl", "is-active", "--quiet", u], check=False).returncode == 0 for u in panel_units):
        raise RuntimeError("Ни одна служба 3x-ui не active после восстановления")
    if _service_exists("xray.service") and subprocess.run(["systemctl", "is-active", "--quiet", "xray.service"], check=False).returncode != 0:
        raise RuntimeError("xray.service не active после восстановления")


def _current_xui_kind() -> str:
    env_path = Path(str(_cfg("XUI_DB_ENV_FILE", "/etc/default/x-ui"))).expanduser()
    values = parse_env(env_path)
    db_type = values.get("XUI_DB_TYPE", "").strip().lower()
    if str(_cfg("XUI_POSTGRES_DSN", "") or "").strip() or str(values.get("XUI_DB_DSN", "") or "").strip() or db_type in {"postgres", "postgresql"}:
        return "PostgreSQL"
    return "SQLite"


def _current_xui_dsn() -> str:
    try:
        from backup import xui_postgres_dsn_for_cli
        return xui_postgres_dsn_for_cli()
    except Exception as exc:
        raise RuntimeError(f"Не удалось определить PostgreSQL DSN 3x-ui: {exc}") from exc


def _snapshot_files(paths: list[tuple[Path, Path]]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="fargovpn_restore_files_"))
    for source, relative in paths:
        if not source.exists():
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)
        elif source.is_dir():
            _copy_tree_safe(source, destination)
    return root


def _copy_tree_safe(source: Path, destination: Path) -> None:
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            src = current_path / dirname
            if src.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в конфигурации: {src}")
        for filename in filenames:
            src = current_path / filename
            if src.is_symlink():
                raise RuntimeError(f"Символическая ссылка запрещена в конфигурации: {src}")
        rel = current_path.relative_to(source)
        dest_dir = destination / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        for dirname in dirnames:
            (dest_dir / dirname).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            shutil.copy2(current_path / filename, dest_dir / filename)
        try:
            shutil.copystat(current_path, dest_dir, follow_symlinks=False)
        except OSError:
            pass


def _restore_tree(snapshot: Path, source: Path, destination: Path) -> None:
    if not source.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists():
            destination.unlink()
        return
    if source.is_dir():
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.mkdir(parents=True, exist_ok=True)
        _copy_tree_safe(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _snapshot_current_state() -> tuple[Path, dict[str, dict[str, bool]]]:
    pairs = [
        (APP_DIR / "config.py", Path("app/config.py")),
        (APP_DIR / ".env", Path("app/.env")),
        (Path("/etc/x-ui"), Path("etc/x-ui")),
        (Path("/etc/xray"), Path("etc/xray")),
        (Path(str(_cfg("XUI_DB_ENV_FILE", "/etc/default/x-ui"))), Path("etc/default/x-ui")),
    ]
    units = [u for u in ("vpn-service-bot.service", "vpn-service-web.service", "vpn-service-web.socket", "vpn-service-backup.service", "vpn-service-backup.timer", "vpn-service-reminders.service", "vpn-service-reminders.timer", "vpn-service-nginx-guard.service", "x-ui.service", "3x-ui.service", "xray.service") if _service_exists(u)]
    unit_paths: list[tuple[Path, Path]] = []
    for unit in units:
        source = Path("/etc/systemd/system") / unit
        if source.is_file():
            unit_paths.append((source, Path("etc/systemd/system") / unit))
    pairs.extend(unit_paths)
    vapid = Path(str(_cfg("PUSH_VAPID_PRIVATE_KEY_PATH", "/var/lib/vpn-service/vapid_private.pem"))).expanduser()
    pairs.append((vapid, Path("runtime/vapid_private.pem")))
    states = _capture_service_states(units)
    return _snapshot_files(pairs), states


def _rollback_files(snapshot: Path) -> None:
    targets = {
        Path("app/config.py"): APP_DIR / "config.py",
        Path("app/.env"): APP_DIR / ".env",
        Path("etc/x-ui"): Path("/etc/x-ui"),
        Path("etc/xray"): Path("/etc/xray"),
        Path("etc/default/x-ui"): Path(str(_cfg("XUI_DB_ENV_FILE", "/etc/default/x-ui"))),
        Path("runtime/vapid_private.pem"): Path(str(_cfg("PUSH_VAPID_PRIVATE_KEY_PATH", "/var/lib/vpn-service/vapid_private.pem"))).expanduser(),
    }
    for source_relative, destination in targets.items():
        _restore_tree(snapshot, snapshot / source_relative, destination)
    unit_destination = Path("/etc/systemd/system")
    unit_destination.mkdir(parents=True, exist_ok=True)
    for pattern in ("vpn-service-*", "fargovpn*", "x-ui*", "xray*", "3x-ui*"):
        for current in unit_destination.glob(pattern):
            if current.is_file() or current.is_symlink():
                current.unlink(missing_ok=True)
    unit_root = snapshot / "etc/systemd/system"
    if unit_root.is_dir():
        for unit in unit_root.iterdir():
            _restore_tree(snapshot, unit, unit_destination / unit.name)
    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True, text=True, timeout=60)


def _restore_fargo_config_from_archive(root: Path) -> None:
    source = root / "bot/config.py"
    if not source.is_file():
        raise RuntimeError("Полный архив не содержит bot/config.py")
    merge_config(APP_DIR / "config.py", source)
    env_source = root / "configs/application.env"
    if not env_source.is_file():
        env_source = root / "bot/.env"
    env_target = APP_DIR / ".env"
    if env_source.is_file():
        env_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(env_source, env_target)
        os.chmod(env_target, 0o600)


def _restore_system_configs(root: Path) -> None:
    # 4.6.13 stored this directory as etc-x-ui; 4.6.14 backups use the same
    # canonical spelling. Keep the alternate legacy spelling accepted as well.
    xui_source = next(
        (candidate for candidate in (root / "system" / "etc-x-ui", root / "system" / "etc-xui") if candidate.is_dir()),
        None,
    )
    if xui_source is None:
        raise RuntimeError("Полный архив не содержит system/etc-x-ui")
    xui_target = Path("/etc/x-ui")
    if xui_target.exists():
        shutil.rmtree(xui_target)
    xui_target.mkdir(parents=True, exist_ok=True)
    _copy_tree_safe(xui_source, xui_target)

    xray_source = root / "system" / "etc-xray"
    xray_target = Path("/etc/xray")
    manifest_data = _load_json_file(root / "manifest.json", "manifest.json")
    archived_xray_expected = bool((manifest_data.get("components") or {}).get("xray_config"))
    if xray_source.is_dir():
        if xray_target.exists():
            shutil.rmtree(xray_target)
        xray_target.mkdir(parents=True, exist_ok=True)
        _copy_tree_safe(xray_source, xray_target)
    elif archived_xray_expected:
        raise RuntimeError("Полный архив заявляет Xray-конфигурацию, но system/etc-xray отсутствует")
    env_source = root / "configs/x-ui-default.env"
    if env_source.is_file():
        target_env = Path(str(_cfg("XUI_DB_ENV_FILE", "/etc/default/x-ui"))).expanduser()
        # DB connection values are local deployment settings and must not be
        # replaced by a backup from another server; all other x-ui environment
        # settings are restored.
        merge_xui_env(target_env, env_source)
    vapid = root / "runtime/vapid_private.pem"
    if vapid.is_file():
        target = Path(str(_cfg("PUSH_VAPID_PRIVATE_KEY_PATH", "/var/lib/vpn-service/vapid_private.pem"))).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vapid, target)
        os.chmod(target, 0o600)


def _normalize_unit_path(text: str) -> str:
    # Backup service files are restored only after replacing the old deployment
    # path with the current application directory. No shell is evaluated here.
    for candidate in ("/root/vpn_bot", str(APP_DIR)):
        if candidate != str(APP_DIR):
            text = text.replace(candidate, str(APP_DIR))
    return text


def _restore_systemd_units(root: Path) -> None:
    systemd = root / "systemd"
    if not systemd.is_dir():
        raise RuntimeError("Полный архив не содержит systemd-конфигурацию")
    destination = Path("/etc/systemd/system")
    destination.mkdir(parents=True, exist_ok=True)
    for item in systemd.iterdir():
        # Old backups must not resurrect the retired configuration-writing guard.
        if item.name == "vpn-service-nginx-guard.service":
            continue
        if not item.is_file():
            continue
        if item.name.startswith("vpn-service-") or item.name in {"x-ui.service", "3x-ui.service", "xray.service"}:
            text = item.read_text(encoding="utf-8")
            destination_path = destination / item.name
            destination_path.write_text(_normalize_unit_path(text), encoding="utf-8")
            os.chmod(destination_path, 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True, text=True, timeout=60)


def _db_swap_begin(target_dsn: str, staging_name: str) -> tuple[str, str]:
    target_name, _user, _host = pg_parts(target_dsn)
    rollback_name = _pg_identifier(f"{target_name}_pre_restore_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"[:63], "rollback-базы")
    run_postgres_sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{target_name}','{staging_name}') AND pid <> pg_backend_pid()")
    run_postgres_sql(f'ALTER DATABASE "{target_name}" RENAME TO "{rollback_name}"')
    try:
        run_postgres_sql(f'ALTER DATABASE "{staging_name}" RENAME TO "{target_name}"')
    except Exception:
        run_postgres_sql(f'ALTER DATABASE "{rollback_name}" RENAME TO "{target_name}"')
        raise
    return target_name, rollback_name


def _db_swap_rollback(target_dsn: str, rollback_name: str) -> None:
    target_name, _user, _host = pg_parts(target_dsn)
    rollback_name = _pg_identifier(rollback_name, "rollback-базы")
    failed_name = _pg_identifier(f"{target_name}_failed_restore_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"[:63], "неудачной базы")
    run_postgres_sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{target_name}' AND pid <> pg_backend_pid()")
    run_postgres_sql(f'ALTER DATABASE "{target_name}" RENAME TO "{failed_name}"')
    run_postgres_sql(f'ALTER DATABASE "{rollback_name}" RENAME TO "{target_name}"')
    logger.error("Неудачная восстановленная БД оставлена под именем %s", failed_name)


def _sqlite_xui_swap(root: Path, archive_db: Path, current_db: Path, suffix: str) -> Path:
    if _sqlite_integrity(archive_db) != "ok":
        raise RuntimeError("Архивная SQLite база 3x-ui не прошла integrity_check")
    current_db.parent.mkdir(parents=True, exist_ok=True)
    rollback = current_db.with_name(f"{current_db.name}.pre_restore_{suffix}")
    if current_db.exists():
        shutil.copy2(current_db, rollback)
    temp = current_db.with_name(f"{current_db.name}.restore_tmp_{os.getpid()}")
    shutil.copy2(archive_db, temp)
    os.chmod(temp, 0o600)
    os.replace(temp, current_db)
    if _sqlite_integrity(current_db) != "ok":
        if rollback.exists():
            shutil.copy2(rollback, current_db)
        else:
            current_db.unlink(missing_ok=True)
        raise RuntimeError("SQLite база 3x-ui после замены не прошла integrity_check")
    return rollback


def _sqlite_xui_rollback(current_db: Path, rollback: Path) -> None:
    if rollback.is_file():
        temp = current_db.with_name(f"{current_db.name}.rollback_tmp_{os.getpid()}")
        shutil.copy2(rollback, temp)
        os.chmod(temp, 0o600)
        os.replace(temp, current_db)
    else:
        current_db.unlink(missing_ok=True)


def _xui_pg_swap(root: Path, manifest: dict[str, Any], target_dsn: str, suffix: str) -> tuple[str, str]:
    staging_dsn, staging_name = make_staging_db(target_dsn, suffix)
    try:
        restore_dump(Path(manifest["_xui_artifact"]), staging_dsn)
        # At least one public table is required for a real 3x-ui dump.
        command_dsn, env = dsn_password_and_cli(staging_dsn)
        probe = subprocess.run(
            ["psql", "--dbname", command_dsn, "--tuples-only", "--no-align", "--command", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"],
            check=True, env=env, capture_output=True, text=True, timeout=120,
        )
        if int((probe.stdout or "0").strip() or "0") <= 0:
            raise RuntimeError("В восстановленной базе 3x-ui PostgreSQL отсутствуют таблицы")
        target, rollback = _db_swap_begin(target_dsn, staging_name)
        staging_name = ""
        return target, rollback
    finally:
        if staging_name:
            drop_database(staging_name)


def create_safety_backup(*, reason: str = "pre-restore") -> Path:
    """Create the same canonical full backup used by the backup service."""
    _refresh_paths_from_config()
    from backup import LOCK_PATH, create_backup
    logger.info("Создаю страховочный полный бэкап перед восстановлением: reason=%s", reason)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Сейчас выполняется другой backup; restore остановлен до появления согласованного safety-backup") from exc
        archive = create_backup()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    if not archive.is_file():
        raise RuntimeError("Страховочный бэкап не создан")
    validate_archive_file(archive, require_xui=True)
    return archive


@contextmanager
def restore_lock():
    _refresh_paths_from_config()
    RESTORE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESTORE_LOCK_PATH.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Другое восстановление уже выполняется") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _restore_users_only(root: Path, manifest: dict[str, Any], safety_archive: Path | None = None) -> dict[str, Any]:
    import config
    target_dsn = str(getattr(config, "DATABASE_URL", "") or "").strip()
    if not target_dsn.startswith("postgresql"):
        raise RuntimeError("Текущая FargoVPN установка не использует PostgreSQL")
    suffix = time.strftime("%Y%m%d_%H%M%S")
    staging_dsn, staging_name = make_staging_db(target_dsn, suffix)
    expected = manifest.get("application_row_counts") or {}
    snapshot_schema = ""
    try:
        stop_services(include_xui=False)
        restore_application_to_staging(root, manifest, staging_dsn)
        staged_counts = app_counts(staging_dsn)
        if expected.get("users") is not None and int(staged_counts.get("users", -1)) != int(expected["users"]):
            raise RuntimeError(f"Проверка users в архиве не прошла: ожидалось {expected['users']}, получено {staged_counts.get('users')}")
        result = _selected_restore_with_snapshot(target_dsn, staging_dsn, expected)
        snapshot_schema = str(result["snapshot_schema"])
        # The health-check is intentionally done while services are still stopped
        # only after the committed database is verified by row counts.
        final_counts = app_counts(target_dsn)
        start_services(full=str(getattr(config, "INSTALL_PROFILE", "full")).lower() == "full")
        snapshot_schema = str(result["snapshot_schema"])
        verify_live(str(getattr(config, "INSTALL_PROFILE", "full")).lower() == "full", Path(config.__file__).resolve())
        _drop_snapshot_schema(target_dsn, snapshot_schema)
        snapshot_schema = ""
        drop_database(staging_name)
        return {"mode": "users-only", "row_counts": final_counts, "status": "verified", "safety_archive": str(safety_archive or "")}
    except Exception:
        try:
            stop_services(include_xui=False)
        except Exception:
            pass
        if snapshot_schema:
            try:
                _restore_selected_snapshot(target_dsn, snapshot_schema)
            except Exception as rollback_error:
                logger.exception("Откат users-only не удался")
                raise RuntimeError(f"Ошибка восстановления и отката users-only: {rollback_error}") from rollback_error
        drop_database(staging_name)
        try:
            start_services(full=str(getattr(config, "INSTALL_PROFILE", "full")).lower() == "full")
        except Exception:
            logger.exception("Не удалось перезапустить FargoVPN после неудачного users-only restore")
        raise


def _restore_full(root: Path, manifest: dict[str, Any], safety_archive: Path | None = None) -> dict[str, Any]:
    import config
    target_dsn = str(getattr(config, "DATABASE_URL", "") or "").strip()
    if not target_dsn.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL FargoVPN не настроен на PostgreSQL")
    archived_xui_kind = str(manifest["_xui_kind"])
    current_xui_kind = _current_xui_kind()
    if archived_xui_kind != current_xui_kind:
        raise RuntimeError(
            f"Тип БД 3x-ui не совпадает: в архиве {archived_xui_kind}, на текущем сервере {current_xui_kind}. "
            "Автоматическое преобразование между SQLite и PostgreSQL запрещено."
        )

    suffix = time.strftime("%Y%m%d_%H%M%S")
    app_staging_dsn, app_staging_name = make_staging_db(target_dsn, suffix)
    app_rollback_name = ""
    xui_rollback_name = ""
    xui_rollback_file: Path | None = None
    current_snapshot: Path | None = None
    unit_states: dict[str, dict[str, bool]] = {}
    xui_current_db = Path(str(getattr(config, "XUI_DB_PATH", "/etc/x-ui/x-ui.db"))).expanduser()
    xui_dsn = ""
    try:
        if safety_archive:
            validate_archive_file(safety_archive, require_xui=True)
        current_snapshot, unit_states = _snapshot_current_state()
        write_restore_state(job_id="", status="running", phase="snapshot", progress=8, message="Сохранён снимок текущей конфигурации", mode="full")
        stop_services(include_xui=True)

        restore_application_to_staging(root, manifest, app_staging_dsn)
        staged_counts = app_counts(app_staging_dsn)
        expected = manifest.get("application_row_counts") or {}
        if expected.get("users") is not None and int(staged_counts.get("users", -1)) != int(expected["users"]):
            raise RuntimeError(f"Проверка users после подготовки FargoVPN не прошла: ожидалось {expected['users']}, получено {staged_counts.get('users')}")
        write_restore_state(job_id="", status="running", phase="database", progress=28, message="Базы подготовлены и проверены", mode="full")

        _target_name, app_rollback_name = _db_swap_begin(target_dsn, app_staging_name)
        app_staging_name = ""
        write_restore_state(job_id="", status="running", phase="database", progress=43, message="База FargoVPN заменена; старое состояние сохранено для rollback", mode="full")

        if archived_xui_kind == "PostgreSQL":
            xui_dsn = _current_xui_dsn()
            _xui_target, xui_rollback_name = _xui_pg_swap(root, manifest, xui_dsn, suffix)
        else:
            xui_rollback_file = _sqlite_xui_swap(root, Path(manifest["_xui_artifact"]), xui_current_db, suffix)
        write_restore_state(job_id="", status="running", phase="configuration", progress=58, message="Конфигурации FargoVPN/3x-ui/Xray применяются", mode="full")

        _restore_fargo_config_from_archive(root)
        _restore_system_configs(root)
        _restore_systemd_units(root)
        target_config = APP_DIR / "config.py"
        compile(target_config.read_text(encoding="utf-8"), str(target_config), "exec")

        # Migrations are applied after config restoration. init_db must not be able
        # to recreate a missing users table silently because the staged DB was already validated.
        subprocess.run([sys.executable, str(APP_DIR / "init_db.py")], check=True, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        final_counts = app_counts(target_dsn)
        if expected.get("users") is not None and int(final_counts.get("users", -1)) != int(expected["users"]):
            raise RuntimeError(f"Контрольная проверка users после полного восстановления не прошла: ожидалось {expected['users']}, получено {final_counts.get('users')}")
        write_restore_state(job_id="", status="running", phase="services", progress=78, message="Конфигурация установлена; запускаются bot/web/3x-ui", mode="full")
        start_services(full=True)
        verify_live(True, target_config)
        write_restore_state(job_id="", status="success", phase="done", progress=100, message="Полное восстановление завершено и health-check пройден", mode="full", rollback=False)

        # Keep rollback DBs as forensic/manual safety copies for one retention window;
        # do not destroy the only recovery path immediately after success.
        return {
            "mode": "full",
            "status": "verified",
            "application_row_counts": final_counts,
            "application_rollback_database": app_rollback_name,
            "xui_rollback_database": xui_rollback_name,
            "xui_rollback_file": str(xui_rollback_file) if xui_rollback_file else "",
            "safety_archive": str(safety_archive) if safety_archive else "",
        }
    except Exception as exc:
        logger.exception("Полное восстановление завершилось ошибкой; выполняется откат")
        write_restore_state(job_id="", status="rolling_back", phase="rollback", progress=85, message="Ошибка восстановления; выполняется полный откат", mode="full", error=str(exc))
        rollback_error: Exception | None = None
        try:
            stop_services(include_xui=True)
            if app_rollback_name:
                _db_swap_rollback(target_dsn, app_rollback_name)
            else:
                drop_database(app_staging_name)
            if xui_rollback_name and xui_dsn:
                _db_swap_rollback(xui_dsn, xui_rollback_name)
            elif xui_rollback_file:
                _sqlite_xui_rollback(xui_current_db, xui_rollback_file)
            if current_snapshot:
                _rollback_files(current_snapshot)
            for unit, state in unit_states.items():
                desired = state.get("enabled", False)
                subprocess.run(["systemctl", "enable" if desired else "disable", unit], check=False, capture_output=True, text=True, timeout=30)
            start_services(full=True)
            verify_live(True, APP_DIR / "config.py")
            write_restore_state(job_id="", status="failed", phase="rollback_done", progress=100, message="Восстановление не применено; исходное состояние возвращено", mode="full", error=str(exc), rollback=True)
        except Exception as rollback_exc:  # noqa: BLE001
            rollback_error = rollback_exc
            logger.exception("Критическая ошибка rollback полного восстановления")
            write_restore_state(job_id="", status="failed", phase="rollback_failed", progress=100, message="Исходное состояние не удалось полностью вернуть", mode="full", error=f"{exc}; rollback: {rollback_exc}", rollback=False)
        if rollback_error:
            raise RuntimeError(f"Восстановление завершилось ошибкой: {exc}. Откат также завершился ошибкой: {rollback_error}") from rollback_error
        raise
    finally:
        if app_staging_name:
            drop_database(app_staging_name)
        if current_snapshot:
            shutil.rmtree(current_snapshot, ignore_errors=True)


def run_restore(
    archive: Path,
    *,
    mode: str,
    job_id: str = "",
    safety_archive: Path | None = None,
    create_safety: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    _refresh_paths_from_config()
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise RuntimeError(f"Архив не найден: {archive}")
    mode = str(mode).strip().lower()
    if mode not in {"users-only", "full"}:
        raise RuntimeError("Режим восстановления должен быть users-only или full")
    with restore_lock():
        write_restore_state(job_id=job_id, status="validating", phase="archive", progress=3, message="Проверка архива перед восстановлением", mode=mode, archive=str(archive))
        with tempfile.TemporaryDirectory(prefix="fargovpn_restore_job_") as temp:
            root = safe_extract(archive, Path(temp))
            manifest = verify_archive(root, require_xui=(mode == "full"))
            if safety_archive is None and create_safety:
                write_restore_state(job_id=job_id, status="running", phase="safety-backup", progress=5, message="Создаётся страховочный бэкап текущего состояния", mode=mode, archive=str(archive))
                safety_archive = create_safety_backup(reason="pre-restore")
            if safety_archive is not None:
                validate_archive_file(Path(safety_archive).resolve(), require_xui=True)
            write_restore_state(job_id=job_id, status="running", phase="confirm-gate", progress=6, message="Предварительные проверки завершены", mode=mode, archive=str(archive), extra={"safety_archive": str(safety_archive or ""), "actor": actor})
            try:
                if mode == "users-only":
                    result = _restore_users_only(root, manifest, safety_archive=safety_archive)
                else:
                    result = _restore_full(root, manifest, safety_archive=safety_archive)
                result.update({"job_id": job_id, "archive": str(archive), "actor": actor, "safety_archive": str(safety_archive or "")})
                return result
            except Exception as exc:
                if mode == "users-only":
                    write_restore_state(job_id=job_id, status="failed", phase="error", progress=100, message="Users-only восстановление не применено", mode=mode, archive=str(archive), error=str(exc), rollback=True)
                raise


def create_safety_only(*, json_mode: bool = False) -> int:
    archive = create_safety_backup(reason="cli-pre-restore")
    data = {"status": "created", "archive": str(archive), "manifest": validate_archive_file(archive, require_xui=True)}
    if json_mode:
        json_out(data)
    else:
        print(archive)
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-dir", type=Path, help="Каталог, где лежит цельный архив или его части")
    parser.add_argument("--work-dir", type=Path, help="Рабочий каталог для reassemble/extract")
    parser.add_argument("--users-only", type=Path, help="Цельный .tar.gz для users-only восстановления")
    parser.add_argument("--full", type=Path, help="Цельный .tar.gz для полного восстановления")
    parser.add_argument("--safety-archive", type=Path, help="Уже созданный страховочный архив текущего состояния")
    parser.add_argument("--no-safety", action="store_true", help="Не создавать страховочный архив; только для внутреннего orchestrator")
    parser.add_argument("--safety-backup", action="store_true", help="Создать и проверить страховочный полный архив текущего состояния")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--actor", default="")
    args = parser.parse_args()
    if args.safety_backup:
        return create_safety_only(json_mode=args.json)
    if args.prepare_dir:
        if not args.work_dir:
            raise SystemExit("--prepare-dir требует --work-dir")
        report = prepare(args.prepare_dir, args.work_dir)
        json_out(report)
        return 0
    if bool(args.users_only) == bool(args.full):
        raise SystemExit("Укажите ровно один из --users-only или --full")
    archive = (args.users_only or args.full).expanduser().resolve()
    mode = "users-only" if args.users_only else "full"
    result = run_restore(
        archive,
        mode=mode,
        job_id=str(args.job_id),
        safety_archive=args.safety_archive.expanduser().resolve() if args.safety_archive else None,
        create_safety=not args.no_safety,
        actor=str(args.actor),
    )
    json_out(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except Exception as exc:  # noqa: BLE001
        logger.exception("ОШИБКА ВОССТАНОВЛЕНИЯ: %s", exc)
        print(f"ОШИБКА ВОССТАНОВЛЕНИЯ: {exc}", file=sys.stderr)
        raise SystemExit(1)
