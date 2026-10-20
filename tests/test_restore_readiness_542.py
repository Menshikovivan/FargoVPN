"""Actual restore health polling with deterministic time and curl boundaries."""
import ast
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0 <= seconds <= 1
        self.now += seconds


def helper(clock, run):
    source = Path(__file__).resolve().parents[1] / 'app/restore_manager.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == '_wait_web_health')
    namespace = {'Path': Path, 'time': clock,
                 'subprocess': SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_wait_web_health']


class Socket:
    def __init__(self, clock, ready_at=0):
        self.clock = clock
        self.ready_at = ready_at

    def exists(self):
        return self.clock.now >= self.ready_at

    def __str__(self):
        return '/tmp/a26-synthetic.sock'


@pytest.mark.parametrize('scenario', ['delayed_http', 'delayed_socket', 'fallback',
                                     'permanent_failure', 'subprocess_timeout'])
def test_restore_waits_for_real_health_and_preserves_deadline(scenario):
    clock = Clock()
    calls = []
    budget = 12 if scenario == 'subprocess_timeout' else 4

    def run(command, **kwargs):
        before = clock.now
        assert command[:3] == ['curl', '--unix-socket', '/tmp/a26-synthetic.sock']
        assert 0 < float(command[command.index('--max-time') + 1]) <= 5
        assert 0 < kwargs['timeout'] <= min(10, budget - before)
        calls.append((before, command[-1]))
        if scenario == 'subprocess_timeout' and len(calls) == 1:
            clock.now += kwargs['timeout']
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        clock.now += min(0.2, budget - before)
        ready = (scenario == 'delayed_http' and before >= 2
                 or scenario == 'delayed_socket'
                 or scenario == 'fallback' and command[-1].endswith('/health')
                 or scenario == 'subprocess_timeout')
        return SimpleNamespace(returncode=0 if ready else 7,
                               stdout='OK synthetic service' if ready else '')

    socket = Socket(clock, ready_at=2 if scenario == 'delayed_socket' else 0)
    wait = helper(clock, run)
    if scenario == 'permanent_failure':
        with pytest.raises(RuntimeError, match='Health-check'):
            wait(socket, timeout=budget)
        assert abs(clock.now - budget) < 1e-8
    else:
        wait(socket, timeout=budget)
        assert clock.now <= budget
    if scenario == 'delayed_http':
        assert len(calls) > 2 and calls[-1][0] >= 2
    elif scenario == 'delayed_socket':
        assert calls and all(at >= 2 for at, _ in calls)
    elif scenario == 'fallback':
        assert [url.rsplit('/', 1)[-1] for _, url in calls] == ['healthz', 'health']
    elif scenario == 'subprocess_timeout':
        assert len(calls) == 2 and calls[1][0] == 10


@pytest.mark.parametrize('scenario', ['empty_token', 'bot_exits_during_web_wait', 'stable_units'])
def test_verify_live_keeps_bot_gate_after_web_readiness(tmp_path, scenario):
    source = Path(__file__).resolve().parents[1] / 'app/restore_manager.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'verify_live')
    config_path = tmp_path / 'config.py'
    config_path.write_text('# synthetic config boundary\n')
    checks = []
    state = {'bot_alive': True, 'wait_ran': False}

    def unit_active(unit):
        checks.append(unit)
        if unit == 'vpn-service-bot.service' and not state['bot_alive']:
            raise RuntimeError('Synthetic bot exited during ASGI warm-up')

    def web_ready(socket):
        state['wait_ran'] = True
        if scenario == 'bot_exits_during_web_wait':
            state['bot_alive'] = False

    namespace = {'Path': Path, 'APP_DIR': tmp_path,
                 'parse_literal_config': lambda path: {'BOT_TOKEN': '' if scenario == 'empty_token' else 'synthetic-not-a-real-token'},
                 '_verify_unit_active': unit_active, '_wait_web_health': web_ready,
                 'shutil': SimpleNamespace(which=lambda name: '/usr/bin/curl'),
                 '_service_exists': lambda unit: False}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    verify = namespace['verify_live']
    if scenario == 'empty_token':
        with pytest.raises(RuntimeError, match='BOT_TOKEN'):
            verify(True, config_path)
        assert checks == [] and not state['wait_ran']
    elif scenario == 'bot_exits_during_web_wait':
        with pytest.raises(RuntimeError, match='Synthetic bot exited'):
            verify(True, config_path)
        assert state['wait_ran'] and checks.count('vpn-service-bot.service') == 2
    else:
        verify(True, config_path)
        assert state['wait_ran'] and checks.count('vpn-service-bot.service') == 2
        assert checks.count('vpn-service-web.socket') == 2
        assert checks.count('vpn-service-web.service') == 2
