import hashlib
import importlib.util
import logging
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

module_path = Path(__file__).resolve().parents[1] / 'app/restore_manager.py'
spec = importlib.util.spec_from_file_location('restore_compat_test', module_path)
m = importlib.util.module_from_spec(spec)
with patch('logging.FileHandler', return_value=logging.NullHandler()):
    spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as directory:
    dump = Path(directory) / 'dump.sql'
    body = b"--\n-- PostgreSQL database dump\n--\nSET transaction_timeout = 0;\n-- Name: users; Type: TABLE\nCOPY users FROM stdin;\nSET transaction_timeout = 0;\n\\.\n"
    dump.write_bytes(body)
    for version in (160000, 170000, 180000):
        with m.compatible_plain_dump(dump, version) as output:
            result = output.read_bytes()
            assert result.count(b'SET transaction_timeout = 0;') == (1 if version < 170000 else 2)
            assert result[result.index(b'-- Name:'):] == body[body.index(b'-- Name:'):]
        assert dump.read_bytes() == body
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if 'SHOW server_version_num' in command:
            return subprocess.CompletedProcess(command, 0, stdout='160000\n')
        assert 'ON_ERROR_STOP=1' in command
        assert Path(command[-1]).read_bytes().count(b'SET transaction_timeout = 0;') == 1
        raise subprocess.CalledProcessError(3, command, stderr='deliberate SQL error')
    with patch.object(m.shutil, 'which', return_value='/usr/bin/psql'), patch.object(m, 'dsn_password_and_cli', return_value=('postgresql://localhost/test', {})), patch.object(m.subprocess, 'run', side_effect=run):
        try:
            m._restore_trusted_schema(dump, 'mock')
        except RuntimeError as exc:
            assert 'deliberate SQL error' in str(exc)
        else:
            raise AssertionError('SQL errors must still abort restoration')
print('Version compatibility, data preservation, source preservation, SQL error handling: PASS')
