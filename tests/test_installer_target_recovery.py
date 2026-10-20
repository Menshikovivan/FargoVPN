"""Real shell recovery functions; only systemctl is replaced by a recorder."""
from pathlib import Path
import os
import re
import subprocess
import sys

import pytest


def _function(source: str, name: str) -> str:
    match = re.search(r'(?ms)^' + re.escape(name) + r'\(\) \{\n.*?^\}', source)
    assert match, name
    return match.group(0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux bash / executable / canonical paths')
@pytest.mark.parametrize('case', ['healthy', 'invalid', 'unrelated', 'unknown', 'matching_broken'])
def test_recovery_mutates_only_validated_target_units(tmp_path, case):
    script = Path(__file__).resolve().parents[1] / 'app/install.sh'
    source = script.read_text(encoding='utf-8')
    target = tmp_path / 'target'
    other = tmp_path / 'other'
    for directory in (target, other):
        directory.mkdir()
        (directory / 'main.py').write_text('# synthetic\n')
        (directory / 'config.py').write_text('# synthetic\n')
    if case == 'healthy':
        python = target / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nexit 0\n')
        python.chmod(0o700)
    action_log = tmp_path / 'systemctl-actions'
    env = dict(os.environ, TARGET=str(target if case != 'invalid' else tmp_path / 'missing'),
               PRIMARY_WD=str(target), ALIAS_WD=str(other), ACTION_LOG=str(action_log))
    if case == 'unrelated':
        env['PRIMARY_WD'] = str(other)
    elif case == 'unknown':
        env['PRIMARY_WD'] = ''
        env['ALIAS_WD'] = str(tmp_path / 'nonexistent')
    shell = '''set -Eeuo pipefail
systemctl() {
  if [[ "$1" == show ]]; then
    case "${@: -1}" in
      vpn-service-web.service) printf '%s' "$PRIMARY_WD" ;;
      fargovpn-web.service) printf '%s' "$ALIAS_WD" ;;
      *) return 1 ;;
    esac
  else
    printf '%s\\n' "$*" >> "$ACTION_LOG"
  fi
}
'''
    shell += _function(source, 'project_dir') + '\n'
    shell += _function(source, 'recover_missing_target_venv') + '\nrecover_missing_target_venv\n'
    completed = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', shell],
                               env=env, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    actions = action_log.read_text().splitlines() if action_log.exists() else []
    assert actions == (['stop vpn-service-web.service vpn-service-web.socket',
                        'disable vpn-service-web.service vpn-service-web.socket']
                       if case == 'matching_broken' else [])
