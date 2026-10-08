#!/usr/bin/env python3
"""Force-refresh the local fallback cache from the live 3x-ui API."""
from __future__ import annotations

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

import config
from services.xui_api import fetch_and_sync


def main() -> int:
    print("🔄 Получение актуальных пользователей и трафика из 3x-ui API...")
    snapshot = fetch_and_sync(force=True, db_path=config.DB_PATH)
    if snapshot.get("stale"):
        print(f"❌ 3x-ui API недоступен: {snapshot.get('error') or 'неизвестная ошибка'}")
        return 1
    print(f"✅ Синхронизировано пользователей: {len(snapshot.get('clients', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
