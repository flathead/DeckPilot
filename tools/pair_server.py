#!/usr/bin/env python3
"""Five-minute LAN pairing server, run by deckpower after setup-linux.sh."""
import hashlib
import hmac
import json
import os
import queue
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import select
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 47833
TIMEOUT = 300
HOME = Path('/var/lib/decky-power')
AUTH = HOME / '.ssh' / 'authorized_keys'
MAX_TRIES = 8


def fingerprint(key):
    result = subprocess.run(['/usr/bin/ssh-keygen', '-lf', '-'], input=key + '\n',
                            text=True, capture_output=True, timeout=4)
    if result.returncode:
        raise ValueError('invalid ssh-ed25519 public key')
    parts = result.stdout.split()
    if len(parts) < 2 or not parts[1].startswith('SHA256:'):
        raise ValueError('no valid SSH fingerprint')
    return parts[1]


def write_authorized(key):
    # The account dispatcher accepts a fixed set of commands.
    prefixed = 'restrict,command="/usr/local/libexec/decky-power-command" ' + key + ' decky-power\n'
    AUTH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    AUTH.parent.chmod(0o700)
    existing = AUTH.read_text() if AUTH.is_file() else ''
    # Preserve any other authorized users and previous installations.
    lines = [l for l in existing.splitlines() if not (l.startswith('restrict,command="/usr/local/libexec/decky-power-command" ') and key.split()[1] in l)]
    lines.append(prefixed.rstrip('\n'))
    fd, tmp = tempfile.mkstemp(prefix='.authorized-', dir=AUTH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, AUTH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    def __init__(self):
        self.code = f'{secrets.randbelow(100000000):08d}'
        self.deadline = time.monotonic() + TIMEOUT
        self.retries = 0
        self.lock = threading.Lock()
        self.requests = queue.Queue(maxsize=1)
        self.done = threading.Event()
        self.pending = False
        self.mac = os.environ.get('DECKY_POWER_MAC', '')
        self.ssh_port = int(os.environ.get('DECKY_POWER_SSH_PORT', '22'))
        pub = Path('/etc/ssh/ssh_host_ed25519_key.pub')
        self.fp = fingerprint(pub.read_text().strip())
        self.name = socket.gethostname()[:64]


state = None


class Handler(BaseHTTPRequestHandler):
    server_version = 'DeckyPair/0.2.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(75)
    def log_message(self, *_args):
        pass
    def answer(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path != '/info':
            return self.answer(404, {'error':'not found'})
        if time.monotonic() >= state.deadline or state.done.is_set():
            return self.answer(410, {'error':'pairing closed'})
        return self.answer(200, {'protocol':1, 'name':state.name,'mac':state.mac,
                                 'ssh_port':state.ssh_port, 'fingerprint':state.fp})

    def do_POST(self):
        if self.path != '/pair':
            return self.answer(404, {'error':'not found'})
        if state.done.is_set() or time.monotonic() >= state.deadline:
            return self.answer(410, {'error':'pairing closed'})
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not (1 <= size <= 1536):
                return self.answer(413, {'error':'invalid payload'})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('invalid payload')
            supplied = data.get('code', '')
            pub = data.get('public_key', '')
            if not isinstance(supplied, str) or not isinstance(pub, str):
                raise ValueError('malformed payload')
        except (ValueError, TypeError, UnicodeError):
            return self.answer(400, {'error':'invalid request'})
        with state.lock:
            if state.pending:
                return self.answer(409, {'error':'another pairing is awaiting confirmation'})
            state.retries += 1
            if state.retries > MAX_TRIES:
                return self.answer(429, {'error':'too many attempts: restart pairing on PC'})
            if not hmac.compare_digest(state.code, supplied):
                return self.answer(403, {'error':'incorrect pairing code'})
            try:
                pieces = pub.strip().split()
                if len(pieces) < 2 or pieces[0] != 'ssh-ed25519' or len(pieces[1]) > 256:
                    raise ValueError('not a valid ed25519 key')
                key = pieces[0] + ' ' + pieces[1]
                fp = fingerprint(key)
            except (ValueError, subprocess.SubprocessError):
                return self.answer(400, {'error':'invalid public key'})
            pending = {'key':key, 'fp':fp, 'client':self.client_address[0], 'approved':threading.Event(), 'ok':False,
                       'deadline': min(state.deadline, time.monotonic() + 60)}
            try:
                state.requests.put_nowait(pending)
                state.pending = True
            except queue.Full:
                return self.answer(409, {'error':'another pairing is awaiting confirmation'})
        pending['approved'].wait(timeout=65)
        with state.lock:
            state.pending = False
        if not pending['ok']:
            return self.answer(403, {'error':'pairing not approved on computer'})
        state.done.set()
        return self.answer(200, {'success':True, 'fingerprint':state.fp, 'mac':state.mac, 'name':state.name})


def main():
    global state
    if os.geteuid() == 0:
        raise SystemExit('Refusing to run pairing server as root')
    state = State()
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    server = Server(('0.0.0.0',PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print('\n'+'='*56)
    print('DeckPilot: временное сопряжение (5 минут)')
    print('КОД В DECKY: ' + state.code)
    print('ОТПЕЧАТОК SSH ПК: ' + state.fp)
    print('MAC (Ethernet): ' + (state.mac or 'не определён'))
    print('На Steam Deck: Decky → DeckPilot → Подключить ПК.')
    print('После запроса сравни короткий код сверки и подтверди YES.')
    print('='*56+'\n',flush=True)
    try:
        while time.monotonic() < state.deadline and not state.done.is_set():
            try:
                pending = state.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            print(f'\nЗАПРОС НА ПОДКЛЮЧЕНИЕ от {pending["client"]}')
            digest = hashlib.sha256((state.fp + '|' + pending['fp']).encode('ascii')).hexdigest()[:12].upper()
            short = '-'.join(digest[i:i+4] for i in range(0,12,4))
            print('КОД СВЕРКИ:', short)
            print('Сравни этот код с кодом сверки в Decky.')
            print('Введи YES для разрешения, иначе нажми Enter:', flush=True)
            try:
                # The script is intentionally interactive; no unattended authorization.
                remaining = max(0, pending['deadline'] - time.monotonic())
                ready, _, _ = select.select([sys.stdin], [], [], remaining)
                answer = input().strip() if ready else ''
            except EOFError:
                answer = ''
            if answer == 'YES' and time.monotonic() < pending['deadline']:
                try:
                    write_authorized(pending['key'])
                    pending['ok'] = True
                    print('Ключ Deck добавлен. Можно завершать настройку в Decky.',flush=True)
                except OSError as exc:
                    print('Ошибка сохранения ключа:',str(exc),flush=True)
            else:
                print('Запрос отклонён.',flush=True)
            pending['approved'].set()
            if pending['ok']:
                # Allow the HTTP handler to send its success response before shutdown.
                state.done.wait(timeout=4)
                break
        if not state.done.is_set():
            print('Время сопряжения истекло или запрос не подтверждён. Запусти команду снова.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
