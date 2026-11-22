import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("bash"), reason="requires Bash")
@pytest.mark.parametrize("update_path", ["", "/root/vpn_bot"])
def test_sudo_rs_stream_does_not_wait_at_the_interactive_menu(update_path):
    source = (Path(__file__).resolve().parents[1] / "app/install.sh").read_text(encoding="utf-8")
    start = source.index("INPUT_FD=0")
    end = source.index("\nread_reply()", start)
    probe = '\n'.join((
        'sudo() { printf "sudo-rs 0.2.13\\n"; }',
        source[start:end],
        'printf "READY\\n"',
    ))
    env = dict(os.environ, UPDATE_PATH=update_path, SUDO_COMMAND="/usr/bin/bash")
    result = subprocess.run(["bash", "-c", probe], input="", capture_output=True,
                            encoding="utf-8", env=env, timeout=6, check=False)
    if update_path:
        assert result.returncode == 0 and result.stdout.strip() == "READY"
    else:
        assert result.returncode == 2
        assert "sudo-rs" in result.stderr
        assert 'sudo bash "$bootstrap"' in result.stderr
        assert "READY" not in result.stdout
