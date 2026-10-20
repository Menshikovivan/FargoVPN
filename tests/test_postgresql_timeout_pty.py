"""Exercise the actual PostgreSQL shell timeout helper under a controlling PTY."""
from pathlib import Path
import os
import select
import signal
import sys
import time

import pytest


def _run_helper_under_pty(helper: str) -> tuple[int, bytes]:
    import pty

    shell = (
        "set -Eeuo pipefail\n" + helper + "\n"
        "printf 'A26_PTY_READY\\n'\n"
        "result=\"$(run_timed 2 'synthetic PTY stdin' bash -c "
        "'IFS= read -r value; [[ \"$value\" == a26-tty-canary ]]' | cat)\"\n"
        "printf 'A26_PTY_CHILD_READ_OK\\n'\n"
    )
    pid, master = pty.fork()
    if pid == 0:
        os.execv('/bin/bash', ['bash', '--noprofile', '--norc', '-c', shell])
    output = bytearray()
    sent = False
    status = None
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b''
                output.extend(chunk)
                if b'A26_PTY_READY' in output and not sent:
                    os.write(master, b'a26-tty-canary\n')
                    sent = True
            finished, value = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = value
                break
        if status is None:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
            raise AssertionError('Bounded PTY helper did not finish')
        return os.waitstatus_to_exitcode(status), bytes(output)
    finally:
        os.close(master)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux controlling PTY job-control regression')
def test_postgresql_timeout_helper_keeps_child_in_foreground():
    source = Path(__file__).resolve().parents[1] / 'app/scripts/setup_postgresql.sh'
    helper = next(line for line in source.read_text(encoding='utf-8').splitlines()
                  if line.startswith('run_timed(){'))
    code, output = _run_helper_under_pty(helper)
    assert code == 0, (code, output.decode('utf-8', errors='replace'))
    assert b'A26_PTY_CHILD_READ_OK' in output
