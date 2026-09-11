"""Two-way HTTP load lab. Python standard library only; single process per app."""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import random
import resource
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

APP = os.getenv('APP_NAME', 'app-a')
PEER = os.getenv('PEER_URL', 'http://app-b:8000').rstrip('/')
PORT = int(os.getenv('PORT', '8000'))
START = time.time()
LOCK = threading.RLock()
CONFIG_LOCK = threading.Lock()
STOP = threading.Event()
CONFIG = dict(rps=5, cpu_ms=5, memory_mb=16, delay_ms=0, error_rate=0)
MEMORY = bytearray(16 * 1024 * 1024)
COUNTERS = dict(ok=0, error=0, sent_ok=0, sent_error=0, dropped=0)
BUCKETS = (.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)
HIST = [0] * len(BUCKETS)
LATENCY_SUM = 0.0
LATENCY_COUNT = 0
INFLIGHT = 0
SLOTS = threading.BoundedSemaphore(16)
INBOUND = threading.BoundedSemaphore(16)


def log(event, **fields):
    print(json.dumps(dict(time=time.time(), app=APP, event=event, **fields)), flush=True)


def read_number(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def metrics():
    with LOCK:
        counts, hist, config = COUNTERS.copy(), HIST.copy(), CONFIG.copy()
        total, number, inflight = LATENCY_SUM, LATENCY_COUNT, INFLIGHT
    lines = []

    def add(name, kind, value, labels=''):
        if value is not None:
            lines.extend([f'# TYPE {name} {kind}', f'{name}{labels} {value}'])

    lines.append('# TYPE lab_requests_total counter')
    for status, key in [('200', 'ok'), ('503', 'error')]:
        lines.append(f'lab_requests_total{{status="{status}"}} {counts[key]}')
    lines.append('# TYPE lab_outgoing_requests_total counter')
    for result, key in [('success', 'sent_ok'), ('error', 'sent_error')]:
        lines.append(f'lab_outgoing_requests_total{{result="{result}"}} {counts[key]}')
    add('lab_load_dropped_total', 'counter', counts['dropped'])
    add('lab_outgoing_inflight', 'gauge', inflight)
    lines.append('# TYPE lab_request_duration_seconds histogram')
    for bound, value in zip(BUCKETS, hist):
        lines.append(f'lab_request_duration_seconds_bucket{{le="{bound}"}} {value}')
    lines.extend([f'lab_request_duration_seconds_bucket{{le="+Inf"}} {number}',
                  f'lab_request_duration_seconds_sum {total}',
                  f'lab_request_duration_seconds_count {number}'])
    for key, value in config.items():
        add('lab_config_' + key, 'gauge', value)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    add('process_cpu_seconds_total', 'counter', usage.ru_utime + usage.ru_stime)
    add('process_start_time_seconds', 'gauge', START)
    try:
        rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        add('process_resident_memory_bytes', 'gauge', rss)
    except (OSError, ValueError, IndexError):
        pass
    # Docker's private cgroup namespace exposes the container cgroup at this root.
    cg = Path('/sys/fs/cgroup')
    available = (cg / 'cgroup.controllers').exists()
    add('lab_cgroup_v2_available', 'gauge', int(available))
    if available:
        add('lab_container_memory_bytes', 'gauge', read_number(cg / 'memory.current'))
        add('lab_container_memory_limit_bytes', 'gauge', read_number(cg / 'memory.max'))
        try:
            stats = dict(line.split() for line in (cg / 'cpu.stat').read_text().splitlines())
            add('lab_container_cpu_seconds_total', 'counter', int(stats['usage_usec']) / 1e6)
            add('lab_container_throttled_seconds_total', 'counter', int(stats.get('throttled_usec', 0)) / 1e6)
            quota, period = (cg / 'cpu.max').read_text().split()
            if quota != 'max':
                add('lab_container_cpu_limit_cores', 'gauge', int(quota) / int(period))
        except (OSError, ValueError, KeyError):
            pass
    return ('\n'.join(lines) + '\n').encode()


def update_config(data):
    global MEMORY
    bounds = dict(rps=(0, 500), cpu_ms=(0, 500), memory_mb=(0, 160),
                  delay_ms=(0, 3000), error_rate=(0, 1))
    if not isinstance(data, dict) or not data or set(data) - set(bounds):
        raise ValueError('Allowed keys: ' + ', '.join(bounds))
    for key, value in data.items():
        lo, hi = bounds[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f'{key} must be a finite number between {lo} and {hi}')
        if key == 'memory_mb' and not isinstance(value, int):
            raise ValueError('memory_mb must be an integer')
    with CONFIG_LOCK:
        if 'memory_mb' in data:
            # Release the previous buffer before allocating, preventing transient double allocation.
            MEMORY = None
            MEMORY = bytearray(data['memory_mb'] * 1024 * 1024)
            for offset in range(0, len(MEMORY), 4096):
                MEMORY[offset] = 1
        with LOCK:
            CONFIG.update(data)
            result = CONFIG.copy()
    log('config_changed', config=result)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload, content_type='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        global LATENCY_COUNT, LATENCY_SUM
        path = self.path.split('?')[0]
        if path == '/metrics':
            return self.reply(200, metrics(), 'text/plain; version=0.0.4; charset=utf-8')
        if path == '/health':
            return self.reply(200, dict(status='ok', app=APP))
        if path in ('/', '/config'):
            with LOCK:
                config = CONFIG.copy()
            return self.reply(200, dict(app=APP, peer=PEER, config=config))
        if path != '/work':
            return self.reply(404, dict(error='not found'))
        started = time.perf_counter()
        accepted = INBOUND.acquire(blocking=False)
        status = 503
        try:
            if accepted:
                with LOCK:
                    config = CONFIG.copy()
                # thread_time measures CPU time, so throttling increases wall-clock latency.
                deadline = time.thread_time() + config['cpu_ms'] / 1000
                while time.thread_time() < deadline:
                    sum(i * i for i in range(300))
                time.sleep(config['delay_ms'] / 1000)
                status = 503 if random.random() < config['error_rate'] else 200
        finally:
            if accepted:
                INBOUND.release()
        elapsed = time.perf_counter() - started
        with LOCK:
            COUNTERS['ok' if status == 200 else 'error'] += 1
            LATENCY_COUNT += 1
            LATENCY_SUM += elapsed
            for index, bound in enumerate(BUCKETS):
                if elapsed <= bound:
                    HIST[index] += 1
        # /work never calls the peer: background generators provide both directions.
        self.reply(status, dict(app=APP, duration_ms=round(elapsed * 1000, 2)))

    def do_POST(self):
        if self.path != '/config':
            return self.reply(404, dict(error='not found'))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('JSON body must be 1..4096 bytes')
            data = json.loads(self.rfile.read(length))
            self.reply(200, update_config(data))
        except (ValueError, UnicodeDecodeError) as exc:
            self.reply(400, dict(error=str(exc)))


def send_request():
    global INFLIGHT
    result = 'sent_error'
    with LOCK:
        INFLIGHT += 1
    try:
        with build_opener(ProxyHandler({})).open(PEER + '/work', timeout=5) as response:
            response.read()
            result = 'sent_ok' if response.status == 200 else 'sent_error'
    except (OSError, HTTPError):
        pass
    finally:
        with LOCK:
            INFLIGHT -= 1
            COUNTERS[result] += 1
        SLOTS.release()


def generator():
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while not STOP.is_set():
            with LOCK:
                rps = CONFIG['rps']
            if rps == 0:
                STOP.wait(.1)
                continue
            if SLOTS.acquire(blocking=False):
                pool.submit(send_request)
            else:
                with LOCK:
                    COUNTERS['dropped'] += 1
            STOP.wait(1 / rps)


def reporter():
    while not STOP.wait(10):
        with LOCK:
            snapshot = dict(config=CONFIG.copy(), counters=COUNTERS.copy(), inflight=INFLIGHT)
        log('summary', **snapshot)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address


if __name__ == '__main__':
    server = Server(('0.0.0.0', PORT), Handler)
    threading.Thread(target=generator, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()
    log('started', port=PORT, peer=PEER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        server.server_close()
