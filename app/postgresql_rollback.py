"""Installer-only PostgreSQL public-schema snapshot and transactional rollback.

Uses the installed application's credentials; never changes roles or other schemas.
All files containing credentials/data are private. No installer/systemd side effects.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit


def configured_dsn(app: Path) -> str:
    config = app / "config.py"
    if not config.exists():
        return ""
    tree = ast.parse(config.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DATABASE_URL" for t in node.targets):
            value = ast.literal_eval(node.value)
            return str(value or "").strip()
    return ""


def connection(dsn: str) -> tuple[str, dict[str, str]]:
    from urllib.parse import unquote
    parsed = urlsplit(dsn.replace("postgresql+psycopg://", "postgresql://", 1).replace("postgresql+psycopg2://", "postgresql://", 1))
    if parsed.scheme != "postgresql" or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("Rollback requires an explicit PostgreSQL URL with host/database")
    env = os.environ.copy()
    env["PGCONNECT_TIMEOUT"] = "10"
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    # Keep connection options (SSL etc.), but never place passwords in argv.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if parsed.username is not None:
        authority = parsed.username + "@" + authority
    safe = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))
    if "password" in parsed.query.lower():
        raise ValueError("Password query parameters are unsupported; use URL credentials")
    return safe, env


def private_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def require_private(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Rollback inputs must be regular private files")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("Rollback inputs require current-user ownership and mode 0600")


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def snapshot(app: Path, prefix: Path) -> None:
    dsn = configured_dsn(app)
    metadata = prefix.with_suffix(".json")
    if not dsn:
        private_write(metadata, json.dumps({"postgresql": False}))
        return
    safe, env = connection(dsn)
    dump = prefix.with_suffix(".sql")
    part = dump.with_suffix(".sql.part")
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            subprocess.run(["pg_dump", "--dbname", safe, "--schema=public", "--format=plain", "--no-owner"],
                           env=env, stdout=output, check=True, timeout=600)
        if part.stat().st_size < 100:
            raise RuntimeError("PostgreSQL rollback dump is incomplete")
        part.replace(dump)
        private_write(metadata, json.dumps({"postgresql": True, "dsn": dsn, "sha256": digest(dump)}))
    finally:
        part.unlink(missing_ok=True)


def restore(prefix: Path) -> None:
    metadata_path = prefix.with_suffix(".json")
    require_private(metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not metadata["postgresql"]:
        return
    safe, env = connection(metadata["dsn"])
    # The metadata cannot redirect the privileged restore to another file.
    dump = prefix.with_suffix(".sql")
    require_private(dump)
    if digest(dump) != metadata.get("sha256"):
        raise ValueError("PostgreSQL rollback snapshot checksum mismatch")
    # Execute the reset and the complete dump in ONE transaction: a failed
    # restore rolls back the reset as well, rather than leaving an empty DB.
    fd, filename = tempfile.mkstemp(prefix=".rollback-", suffix=".sql", dir=dump.parent)
    try:
        with os.fdopen(fd, "wb") as output, dump.open("rb") as original:
            output.write(b"DROP SCHEMA IF EXISTS public CASCADE;\n")
            import shutil
            shutil.copyfileobj(original, output)
        subprocess.run(["psql", "--no-psqlrc", "--dbname", safe, "--single-transaction", "--set", "ON_ERROR_STOP=1", "--file", filename],
                       env=env, check=True, timeout=600)
    finally:
        Path(filename).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "restore"))
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--app", type=Path)
    args = parser.parse_args()
    if args.action == "snapshot":
        if args.app is None:
            parser.error("snapshot requires --app")
        snapshot(args.app, args.prefix)
    else:
        restore(args.prefix)


if __name__ == "__main__":
    main()
