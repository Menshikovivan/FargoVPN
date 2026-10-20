#!/usr/bin/env python3
"""Seed an isolated FargoVPN PostgreSQL QA database; never touches production.

Usage:
  FARGOVPN_DATABASE_URL=postgresql+psycopg://qa:qa@127.0.0.1:5432/fargovpn_qa \
    PYTHONPATH=app python3 tests/qa/seed_test_db.py --allow-disposable-loopback --users 300 --messages 1500 --logs 1200
"""
from __future__ import annotations
import argparse
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
sys.path.insert(0, str(APP))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-disposable-loopback", action="store_true",
                        help="confirm that the named local QA database may be cleared")
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--messages", type=int, default=1500)
    parser.add_argument("--logs", type=int, default=1200)
    args = parser.parse_args()
    dsn = os.getenv("FARGOVPN_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Refusing to run without an explicit FARGOVPN_DATABASE_URL/DATABASE_URL for the QA database")
    if "postgresql" not in dsn.lower():
        raise SystemExit("QA seeder requires PostgreSQL; provide a dedicated postgresql+psycopg DSN")
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(dsn.replace('postgresql+psycopg://', 'postgresql://', 1))
        port = parts.port
    except ValueError:
        raise SystemExit('Refusing a malformed disposable database URL') from None
    database = parts.path.lstrip('/')
    if (parts.scheme != 'postgresql' or parts.query or parts.fragment
            or (port is not None and not 1 <= port <= 65535)
            or not args.allow_disposable_loopback or parts.hostname not in {'localhost', '127.0.0.1', '::1'}
            or not (database.endswith('_qa') or database.startswith('audit542_'))):
        raise SystemExit('Refusing to clear a database without explicit disposable-loopback confirmation and a QA database name')
    if not database.replace('_', '').isalnum() or not database.isascii():
        raise SystemExit('Refusing an ambiguous disposable database name')
    if min(args.users, args.messages, args.logs) < 0 or max(args.users, args.messages, args.logs) > 100_000:
        raise SystemExit('QA row counts must be between 0 and 100000')

    import config
    import init_db
    import db

    config.DATABASE_URL = dsn
    init_db.migrate()
    now = datetime.now(timezone.utc)
    rng = random.Random(512)
    with db.engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM panel_push_logs")
        conn.exec_driver_sql("DELETE FROM audit_log")
        conn.exec_driver_sql("DELETE FROM user_events")
        conn.exec_driver_sql("DELETE FROM users")
        user_rows = []
        for i in range(max(1, args.users)):
            tg_id = 900000000 + i
            user_rows.append({
                "tg_id": tg_id,
                "username": f"qa_user_{i}",
                "display_name": f"QA User {i}",
                "uuid": f"qa-{i:08d}",
                "email": f"qa{i}@example.test",
                "expiry_time": int((now + timedelta(days=30 + (i % 90))).timestamp()),
                "enable": 1 if i % 7 else 0,
                "registered_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            })
        for row in user_rows:
            conn.exec_driver_sql(
                "INSERT INTO users(tg_id,username,display_name,uuid,email,expiry_time,enable,registered_at) VALUES (%(tg_id)s,%(username)s,%(display_name)s,%(uuid)s,%(email)s,%(expiry_time)s,%(enable)s,%(registered_at)s)", row
            )
        for i in range(max(1, args.messages)):
            tg_id = 900000000 + (i % max(1, args.users))
            incoming = i % 2 == 0
            created = (now - timedelta(seconds=args.messages - i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.exec_driver_sql(
                "INSERT INTO user_events(tg_id,username,direction,event_type,text,created_at,message_kind,delivery_status) VALUES (%(tg_id)s,%(username)s,%(direction)s,%(event_type)s,%(text)s,%(created_at)s,%(kind)s,%(status)s)",
                {"tg_id":tg_id,"username":f"qa_user_{tg_id-900000000}","direction":"in" if incoming else "out","event_type":"telegram_message","text":f"QA message {i} спецсимволы <>& кириллица №{i}","created_at":created,"kind":"message","status":"received" if incoming else "delivered"},
            )
        for i in range(max(1, args.users)):
            tg_id = 900000000 + i
            last_id = max(1, ((args.messages - 1 - i) // max(1, args.users)) + 1) if args.messages else 0
            unread = 1 if i % 5 == 0 else 0
            conn.exec_driver_sql(
                "INSERT INTO user_message_state(tg_id,last_read_event_id,last_incoming_event_id,unread_count,last_message_at,last_message_text) VALUES (%(tg_id)s,0,%(last_id)s,%(unread)s,%(created)s,%(text)s) ON CONFLICT (tg_id) DO UPDATE SET last_incoming_event_id=EXCLUDED.last_incoming_event_id, unread_count=EXCLUDED.unread_count, last_message_at=EXCLUDED.last_message_at, last_message_text=EXCLUDED.last_message_text",
                {"tg_id":tg_id,"last_id":last_id,"unread":unread,"created":now.strftime("%Y-%m-%d %H:%M:%S"),"text":f"QA latest message for {i}"},
            )
        for i in range(max(1, args.logs)):
            conn.exec_driver_sql(
                "INSERT INTO audit_log(created_at,actor,action,details) VALUES (%(created_at)s,%(actor)s,%(action)s,%(details)s)",
                {"created_at":(now-timedelta(seconds=args.logs-i)).strftime("%Y-%m-%d %H:%M:%S"),"actor":"qa_admin","action":"qa_seed","details":f"seed row {i}"},
            )
        conn.commit()
    print(f"seeded users={args.users} messages={args.messages} logs={args.logs} into isolated QA database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
