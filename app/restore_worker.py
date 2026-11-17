#!/usr/bin/env python3
"""Detached worker for web/CLI restore operations."""
from __future__ import annotations

import argparse
import traceback
from pathlib import Path

import restore_manager as manager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("users-only", "full"))
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--actor", default="web")
    parser.add_argument("--safety-archive", type=Path)
    args = parser.parse_args()

    try:
        manager.run_restore(
            args.archive.resolve(),
            mode=args.mode,
            job_id=args.job_id,
            safety_archive=args.safety_archive.resolve() if args.safety_archive else None,
            create_safety=args.safety_archive is None,
            actor=args.actor,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        manager.logger.error("Restore worker failed: %s\n%s", detail, traceback.format_exc())
        state = manager._read_restore_state_raw()
        owner = state.get("job_id")
        if (owner and owner != args.job_id) or (owner == args.job_id and state.get("status") == "failed"):
            return 1
        manager.write_restore_state(
            job_id=args.job_id,
            status="failed",
            phase="error",
            progress=100,
            message="Восстановление завершилось ошибкой; журнал содержит подробности",
            mode=args.mode,
            archive=str(args.archive.resolve()),
            error=detail,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
