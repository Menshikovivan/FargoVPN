#!/usr/bin/env python3
"""Фоновая публикация релиза в GitHub с постоянным статусом для веб-панели."""
from __future__ import annotations

import argparse
import traceback
from datetime import datetime, timezone
from pathlib import Path

import update_manager

def run(job_id: str) -> int:
    manifest = update_manager.read_publish_job_manifest(job_id)
    archive = Path(str(manifest.get("archive") or ""))
    version = str(manifest.get("version") or "")
    original_name = str(manifest.get("original_name") or archive.name)
    try:
        update_manager.write_publish_status(
            "validating", job_id=job_id, version=version, progress=3, phase="startup",
            message="GitHub publisher worker запущен; проверяется архив и конфигурация", error="",
            finished_at="",
        )
        if not archive.is_file():
            raise update_manager.UpdateError("Архив публикации не найден")
        if update_manager.publish_cancel_requested(job_id):
            update_manager.write_publish_status("cancelled", job_id=job_id, version=version, progress=3, phase="cancelled", message="Публикация отменена до обращения к GitHub", error="", finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            return 0
        update_manager.write_publish_status("validating", job_id=job_id, version=version, progress=5, phase="github-auth", message="Проверяется токен и доступ к репозиторию GitHub", error="")
        update_manager.github_validate_configuration()
        update_manager.write_publish_status("validating", job_id=job_id, version=version, progress=8, phase="repository", message="Доступ к ветке main подтверждён; подготовка публикации", error="")
        def progress(percent: int, phase: str, message: str) -> None:
            if update_manager.publish_cancel_requested(job_id):
                raise update_manager.UpdateError("Публикация отменена администратором")
            update_manager.append_publish_log(job_id, f"{phase}: {message}")
            update_manager.write_publish_status(
                "completed" if percent >= 100 else phase,
                job_id=job_id, version=version, progress=percent, phase=phase, message=message, error="",
            )
        result = update_manager.publish_update(archive, original_name, progress=progress)
        try:
            from webapp import _notify_github_publication
            _notify_github_publication(f"Версия {version} успешно загружена на GitHub", str(result.get("github_main_commit_url") or ""))
        except Exception:
            traceback.print_exc()
        update_manager.write_publish_status(
            "completed", job_id=job_id, version=version, progress=100, phase="complete",
            message=f"GitHub-публикация завершена: {version}", error="",
            github_tag=str(result.get("github_tag") or ""), github_release_url=str(result.get("github_release_url") or ""),
            github_main_commit_sha=str(result.get("github_main_commit_sha") or ""), github_main_commit_url=str(result.get("github_main_commit_url") or ""),
            github_main_stale_paths_removed=int(result.get("github_main_stale_paths_removed") or 0),
            github_release_assets=list(result.get("github_release_assets") or []),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        archive.unlink(missing_ok=True)
        update_manager.clear_publish_cancel(job_id)
        return 0
    except Exception as error:
        if update_manager.publish_cancel_requested(job_id):
            update_manager.write_publish_status(
                "cancelled", job_id=job_id, version=version, progress=max(1, int(update_manager.read_publish_status().get("progress") or 1)), phase="cancelled", message="Публикация отменена администратором", error="", finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            update_manager.clear_publish_cancel(job_id)
            return 0
        update_manager.write_publish_status(
            "failed", job_id=job_id, version=version, progress=max(1, int(update_manager.read_publish_status().get("progress") or 1)),
            phase="failed", message="Публикация GitHub не завершена", error=str(error),
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        update_manager.clear_publish_cancel(job_id)
        traceback.print_exc()
        return 1

def main() -> int:
    parser=argparse.ArgumentParser(description="FargoVPN GitHub publish worker")
    parser.add_argument("--job-id",required=True)
    return run(parser.parse_args().job_id)

if __name__ == "__main__":
    raise SystemExit(main())
