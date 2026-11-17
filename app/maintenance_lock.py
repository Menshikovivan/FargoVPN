"""Serialize mutations of the installed FargoVPN service and its databases."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path("/run/vpn-service-maintenance.lock")
INHERITED_FD_ENV = "FARGOVPN_MAINTENANCE_FD"


class MaintenanceBusyError(RuntimeError):
    """Another maintenance operation holds the installed-service lock."""


@contextmanager
def maintenance_lock(*, inherit: bool = False):
    import fcntl

    inherited = os.environ.get(INHERITED_FD_ENV) if inherit else None
    if inherited is None:
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    else:
        try:
            fd = int(inherited)
            if fd < 3 or not os.path.samestat(os.fstat(fd), LOCK_PATH.stat()):
                raise ValueError("wrong lock file")
        except (ValueError, OSError) as exc:
            raise RuntimeError("Не удалось проверить блокировку установщика") from exc

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusyError(
                "Уже выполняется установка, обновление, откат или восстановление"
            ) from exc
        yield fd
    finally:
        # The installer can inherit the same open file description.
        # Closing our descriptor leaves the lock held until its last owner exits.
        if inherited is None:
            os.close(fd)


def ensure_maintenance_idle() -> None:
    with maintenance_lock():
        pass
