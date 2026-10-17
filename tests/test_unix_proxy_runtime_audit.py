"""Exercise real launcher in a subprocess with a real Unix socket on Linux."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux Unix/systemd listener regression')
@pytest.mark.parametrize('activation', [False, True], ids=['uds', 'systemd-fd'])
def test_real_unix_listener_accepts_nginx_forwarded_metadata(tmp_path, activation):
    shutil.copy(Path(__file__).resolve().parents[1] / 'app/panel_runtime.py', tmp_path / 'panel_runtime.py')
    socket_path = str(tmp_path / 'backend.sock')
    (tmp_path / 'config.py').write_text('WEB_SOCKET_PATH = ' + repr(socket_path) + '\n')
    (tmp_path / 'webapp.py').write_text('''import json
async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await send({'type':'lifespan.startup.complete'})
            else:
                await send({'type':'lifespan.shutdown.complete'})
                return
    body = json.dumps({'scheme':scope['scheme'], 'client':scope['client']}).encode()
    await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
    await send({'type':'http.response.body','body':body})
''')
    driver = tmp_path / 'driver.py'
    driver.write_text('''import os, sys
if len(sys.argv)>1:
    os.dup2(int(sys.argv.pop()), 3)
    os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')
import panel_runtime
panel_runtime.main()
''')
    listener = None
    kwargs = {}
    command = [sys.executable, str(driver)]
    if activation:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(socket_path)
        listener.listen(16)
        command.append(str(listener.fileno()))
        kwargs['pass_fds'] = (listener.fileno(),)
    log = (tmp_path / 'runtime.log').open('w+')
    proc = subprocess.Popen(command, cwd=tmp_path, stdout=log, stderr=log, **kwargs)
    try:
        deadline = time.monotonic() + 20
        while True:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(1)
            try:
                conn.connect(socket_path)
                conn.sendall(b'GET / HTTP/1.1\r\nHost: panel.test\r\nX-Forwarded-Proto: https\r\nX-Forwarded-For: 203.0.113.10\r\nConnection: close\r\n\r\n')
                response = b''
                while chunk := conn.recv(65536):
                    response += chunk
                break
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    log.flush(); log.seek(0)
                    pytest.fail('Real launcher failed: ' + log.read())
                time.sleep(.1)
            finally:
                conn.close()
        # Uvicorn uses chunked transfer; find the JSON object independent of framing.
        start = response.index(b'{')
        payload, _ = json.JSONDecoder().raw_decode(response[start:].decode())
        assert payload['scheme'] == 'https', payload
        assert payload['client'] == ['203.0.113.10', 0], payload
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
        if listener:
            listener.close()
        log.close()
