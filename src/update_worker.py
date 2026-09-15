#!/usr/bin/env python3
"""Отдельный процесс обновления, запускаемый из веб-панели.

Процесс продолжает работать во время перезапуска веб-службы, поэтому браузер
может подключиться повторно и продолжить чтение постоянного файла состояния.
"""
from __future__ import annotations

import os
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import update_manager


def _status(job_id: str, state: str, progress: int, phase: str, message: str, **extra: object) -> None:
    current = update_manager.read_status()
    if current.get("job_id") and current.get("job_id") != job_id:
        raise update_manager.UpdateError("Задача обновления была заменена другой задачей")
    update_manager.write_status(
        state,
        job_id=job_id,
        progress=progress,
        phase=phase,
        message=message,
        **extra,
    )


def run(job_id: str, startup_delay: float = 0.0) -> int:
    log_path = update_manager.update_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] задача {job_id} запущена\n")
        try:
            # Подтверждаем старт до задержки. Браузер успевает увидеть новый job_id,
            # даже если HTTP-соединение оборвётся при последующей остановке web-unit.
            _status(
                job_id,
                "queued",
                3,
                "acknowledged",
                "Команда принята отдельным процессом; веб-панель продолжит следить за установкой",
                worker_pid=os.getpid(),
            )
            if startup_delay > 0:
                time.sleep(min(10.0, max(0.0, float(startup_delay))))
            _status(job_id, "checking", 4, "check", "Проверяется выбранный пакет обновления")
            manifest = update_manager.read_job_manifest(job_id)
            info = manifest.get("update") if isinstance(manifest.get("update"), dict) else {}
            if info:
                info = dict(info)
                version = str(info.get("version") or "")
                installed = update_manager.current_version()
                allow_reinstall = bool(info.get("allow_reinstall"))
                allow_downgrade = bool(info.get("allow_downgrade"))
                if not update_manager.is_newer(version, installed) and not (
                    allow_reinstall
                    and update_manager.version_key(version) == update_manager.version_key(installed)
                ) and not (
                    allow_downgrade
                    and update_manager.version_key(version) < update_manager.version_key(installed)
                ):
                    raise update_manager.UpdateError(
                        f"Версия {version or 'не указана'} не новее установленной {installed}"
                    )
                info["available"] = True
            else:
                info = update_manager.check_available_update(force=True)
                if not info.get("available"):
                    raise update_manager.UpdateError(str(info.get("error") or "Новой версии нет"))
                version = str(info.get("version") or "")
            _status(
                job_id, "downloading", 8, "download",
                ("Проверяется загруженный архив " if info.get("source") == "manual" else "Получение версии ") + version,
                version=version,
            )

            def download_progress(percent: int, message: str) -> None:
                _status(job_id, "downloading", 8 + int(percent * 0.22), "download", message, version=version)

            archive = update_manager.obtain_update_archive(info, progress=download_progress)
            _status(job_id, "verifying", 31, "verify", "Контрольная сумма и структура архива проверены", version=version)

            def extract_progress(percent: int, message: str) -> None:
                _status(job_id, "extracting", 32 + int(percent * 0.16), "extract", message, version=version)

            package_root, archive_info = update_manager.stage_update(archive, progress=extract_progress)
            version = str(archive_info["version"])
            command, environment = update_manager.installer_command(package_root)
            environment["VPN_UPDATE_JOB_ID"] = job_id
            _status(job_id, "installing", 50, "install", "Установщик запущен и выполняет подготовку", version=version)
            log.write("Команда: " + " ".join(command) + "\n")
            log.flush()
            result = subprocess.run(
                command,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                status = update_manager.read_status()
                if status.get("state") == "failed" and status.get("job_id") == job_id:
                    log.write(
                        "Установщик сообщил об обработанной ошибке; постоянное состояние сохранено "
                        f"(код выхода {result.returncode}).\n"
                    )
                    return 1
                raise update_manager.UpdateError(f"Установщик завершился с кодом {result.returncode}")
            status = update_manager.read_status()
            if status.get("state") != "completed":
                _status(job_id, "completed", 100, "complete", "Обновление успешно установлено", version=version)
            log.write(f"[{datetime.now(timezone.utc).isoformat()}] задача {job_id} завершена\n")
            return 0
        except Exception as error:
            log.write(traceback.format_exc() + "\n")
            update_manager.write_status(
                "failed",
                job_id=job_id,
                progress=max(1, int(update_manager.read_status().get("progress") or 1)),
                phase="failed",
                message="Установка не завершена",
                error=str(error),
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            return 1


def main() -> int:
    parser = update_manager.RussianArgumentParser(description="Фоновая установка обновления VPN Service")
    parser.add_argument("--job-id", required=True, metavar="ИДЕНТИФИКАТОР", help="идентификатор задачи обновления")
    parser.add_argument(
        "--startup-delay", type=float, default=0.0, metavar="СЕКУНДЫ",
        help="короткая задержка перед запуском, чтобы веб-панель успела вернуть ответ",
    )
    args = parser.parse_args()
    return run(args.job_id, args.startup_delay)


if __name__ == "__main__":
    raise SystemExit(main())
