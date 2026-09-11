"""Real two-process HTTP smoke test, no Docker or third-party packages needed."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, path, data=None):
    req = Request(f'http://127.0.0.1:{port}{path}',
                  data=None if data is None else json.dumps(data).encode(),
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=5) as response:
        return response.read().decode()


def counter(body, prefix):
    return float(next(line.split()[-1] for line in body.splitlines() if line.startswith(prefix + ' ')))


ports = [free_port(), free_port()]
while ports[0] == ports[1]:
    ports[1] = free_port()
processes = []
try:
    for index, port in enumerate(ports):
        env = dict(os.environ, APP_NAME=f'app-{index}', PORT=str(port),
                   PEER_URL=f'http://127.0.0.1:{ports[1-index]}')
        processes.append(subprocess.Popen([sys.executable, str(ROOT / 'app/main.py')],
                                         env=env, stdout=subprocess.DEVNULL))
    for port in ports:
        for attempt in range(50):
            try:
                request(port, '/health')
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('app failed to start')
    time.sleep(2)
    for port in ports:
        body = request(port, '/metrics')
        assert counter(body, 'lab_outgoing_requests_total{result="success"}') > 0
        assert counter(body, 'lab_requests_total{status="200"}') > 0
        assert counter(body, 'lab_request_duration_seconds_count') == counter(body, 'lab_request_duration_seconds_bucket{le="+Inf"}')
    print('PASS: both apps exchange real HTTP requests and expose metrics')
    request(ports[1], '/config', dict(error_rate=1, memory_mb=32, cpu_ms=10))
    time.sleep(.5)
    assert counter(request(ports[0], '/metrics'), 'lab_outgoing_requests_total{result="error"}') > 0
    assert counter(request(ports[1], '/metrics'), 'lab_config_memory_mb') == 32
    print('PASS: live configuration and injected errors reach peer metrics')
    for payload in [dict(rps=-1), dict(rps=float('nan')), dict(memory_mb=999), dict(unknown=1)]:
        try:
            request(ports[0], '/config', payload)
            raise AssertionError('invalid config accepted')
        except HTTPError as exc:
            assert exc.code == 400
    print('PASS: invalid settings rejected')
    for port in ports:
        request(port, '/config', dict(rps=0, error_rate=0))
    time.sleep(.5)
    before = counter(request(ports[0], '/metrics'), 'lab_requests_total{status="200"}')
    time.sleep(.5)
    assert counter(request(ports[0], '/metrics'), 'lab_requests_total{status="200"}') == before
    print('PASS: load can be stopped without recursive request loops')
finally:
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
