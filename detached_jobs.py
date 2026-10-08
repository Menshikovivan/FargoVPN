"""Надёжный запуск фоновых задач вне cgroup веб-панели.

Веб-служба может быть остановлена во время обновления. Поэтому обычный
``subprocess.Popen`` из uvicorn недостаточен: systemd завершит дочерний процесс
вместе с cgroup службы. Этот модуль запускает worker отдельной systemd-службой
и имеет совместимый fallback для систем, где часть параметров systemd-run не
поддерживается.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence


class DetachedJobError(RuntimeError):
    """Фоновую задачу не удалось передать systemd."""


_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,180}$")


def _service_name(unit: str) -> str:
    return unit if unit.endswith(".service") else f"{unit}.service"


def _loaded(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "show", _service_name(unit), "--property=LoadState", "--value"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() not in {"", "not-found"}


def _quote_unit_arg(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise DetachedJobError("Недопустимый символ в команде фоновой задачи")
    # systemd раскрывает % как спецификатор даже внутри кавычек.
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _runtime_unit(
    unit: str,
    command: Sequence[str],
    *,
    description: str,
    working_directory: Path,
) -> str:
    service = _service_name(unit)
    runtime_path = Path("/run/systemd/system") / service
    exec_start = " ".join(_quote_unit_arg(str(value)) for value in command)
    workdir = _quote_unit_arg(str(working_directory.resolve()))
    safe_description = str(description or "Фоновая задача VPN Service").replace("\n", " ").replace("\r", " ")[:240]
    temporary = runtime_path.with_suffix(runtime_path.suffix + ".tmp")
    temporary.write_text(
        "[Unit]\n"
        f"Description={safe_description}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "User=root\n"
        "Group=root\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Environment=TZ=Asia/Almaty\n"
        f"WorkingDirectory={workdir}\n"
        f"ExecStart={exec_start}\n"
        "Nice=10\n"
        "IOSchedulingClass=best-effort\n"
        "TimeoutStartSec=infinity\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o644)
    temporary.replace(runtime_path)
    reload_result = subprocess.run(
        ["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=20, check=False
    )
    if reload_result.returncode != 0:
        runtime_path.unlink(missing_ok=True)
        raise DetachedJobError((reload_result.stderr or reload_result.stdout).strip() or "systemctl daemon-reload завершился ошибкой")
    start_result = subprocess.run(
        ["systemctl", "--no-block", "start", service],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if start_result.returncode != 0 and not _loaded(unit):
        raise DetachedJobError((start_result.stderr or start_result.stdout).strip() or "systemctl не запустил временную службу")
    return "runtime-unit"


def launch_detached(
    unit: str,
    command: Sequence[str],
    *,
    description: str,
    working_directory: str | Path,
) -> str:
    """Запустить команду отдельной systemd-службой и вернуть способ запуска."""
    unit = str(unit or "").strip()
    if not _UNIT_RE.fullmatch(unit):
        raise DetachedJobError("Некорректное имя фоновой systemd-службы")
    if not command or any(not str(value) for value in command):
        raise DetachedJobError("Команда фоновой задачи пуста")
    executable = Path(str(command[0]))
    if executable.is_absolute() and (not executable.is_file() or not os.access(executable, os.X_OK)):
        raise DetachedJobError(f"Исполняемый файл фоновой задачи не найден: {executable}")
    if not Path("/run/systemd/system").is_dir():
        raise DetachedJobError("systemd не запущен; безопасный фоновый запуск недоступен")

    workdir = Path(working_directory).resolve()
    errors: list[str] = []
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        attempts = (
            [
                systemd_run,
                f"--unit={unit}",
                "--collect",
                "--quiet",
                "--no-block",
                f"--description={description}",
                f"--working-directory={workdir}",
                "--property=Type=oneshot",
                "--property=Nice=10",
                "--property=IOSchedulingClass=best-effort",
                "--property=User=root",
                "--property=Group=root",
                "--property=Environment=PYTHONUNBUFFERED=1",
                "--property=Environment=TZ=Asia/Almaty",
                *map(str, command),
            ],
            [
                systemd_run,
                f"--unit={unit}",
                "--quiet",
                "--no-block",
                "--property=Type=oneshot",
                *map(str, command),
            ],
        )
        for index, args in enumerate(attempts, start=1):
            result = subprocess.run(
                args,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0 or _loaded(unit):
                return "systemd-run" if index == 1 else "systemd-run-compatible"
            detail = (result.stderr or result.stdout or f"код {result.returncode}").strip()
            errors.append(detail[-1200:])

    try:
        return _runtime_unit(
            unit,
            [str(value) for value in command],
            description=description,
            working_directory=workdir,
        )
    except Exception as error:
        errors.append(str(error))
        joined = "; ".join(item for item in errors if item) or "неизвестная ошибка systemd"
        raise DetachedJobError(f"Не удалось запустить отдельную systemd-службу: {joined}") from error
