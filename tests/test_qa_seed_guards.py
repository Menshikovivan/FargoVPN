"""QA must not erase an arbitrary DB merely because an application DSN exists."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

SEED = Path(__file__).resolve().parent / 'qa/seed_test_db.py'


@pytest.mark.parametrize('dsn,flags', [
    ('postgresql://qa:fake@127.0.0.1/fargovpn_qa', []),
    ('postgresql://qa:fake@production.example/fargovpn_qa', ['--allow-disposable-loopback']),
    ('postgresql://qa:fake@127.0.0.1/production', ['--allow-disposable-loopback']),
    ('sqlite:///production.db', ['--allow-disposable-loopback']),
    ('postgresql://qa:fake@127.0.0.1/fargovpn_qa?host=production.example', ['--allow-disposable-loopback']),
    ('postgresql://qa:fake@127.0.0.1/fargovpn_qa?dbname=production', ['--allow-disposable-loopback']),
    ('postgresql://qa:fake@127.0.0.1/fargovpn_qa#other', ['--allow-disposable-loopback']),
    ('postgresql://qa:fake@127.0.0.1:wrong/fargovpn_qa', ['--allow-disposable-loopback']),
])
def test_seeder_refuses_before_importing_config_or_connecting(dsn, flags, tmp_path):
    env = os.environ.copy()
    env['FARGOVPN_DATABASE_URL'] = dsn
    # A network/config failure would be different from a deliberate refusal.
    result = subprocess.run([sys.executable, str(SEED), *flags], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Refusing' in result.stderr or 'requires PostgreSQL' in result.stderr
    assert 'Traceback' not in result.stderr
