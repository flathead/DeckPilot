#!/usr/bin/env bash
# Run on the Linux PC: bash setup-linux.sh
# This standalone script embeds the unprivileged pairing agent below.
set -Eeuo pipefail

if (( EUID != 0 )); then
  exec sudo bash "$0"
fi

printf 'DeckPilot: подключение Linux-ПК к Steam Deck\n\n'
if ! command -v systemctl >/dev/null; then
  echo 'Требуется Linux с systemd (Arch, Ubuntu/Debian, Fedora).' >&2; exit 1
fi
printf 'Мастер настроит отдельный ограниченный SSH-доступ и Wake-on-LAN.\n'
read -r -p 'Продолжить установку? [Y/n]: ' INSTALL_REPLY
[[ "$INSTALL_REPLY" != [nNнН] ]] || exit 0
if command -v pacman >/dev/null; then
  pacman -S --needed --noconfirm openssh ethtool sudo iproute2 python
elif command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y openssh-server openssh-client ethtool sudo iproute2 python3
elif command -v dnf >/dev/null; then
  dnf install -y openssh-server openssh-clients ethtool sudo iproute python3
else
  echo 'Автоустановка пакетов поддерживает pacman, apt-get и dnf.' >&2; exit 1
fi
if id -u deckpower >/dev/null 2>&1; then
  [[ "$(getent passwd deckpower | cut -d: -f6)" == '/var/lib/decky-power' ]] || {
    echo 'Учётная запись deckpower уже существует с другим домашним каталогом.' >&2; exit 1;
  }
fi
if ! id -u deckpower >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/decky-power \
    --shell /bin/sh --password 'NP' deckpower
fi
# A deliberately unusable password hash, not a locked user; key authentication remains possible.
usermod -p 'NP' deckpower
install -d -m 0755 /usr/local/libexec
cat >/usr/local/libexec/decky-power-command <<'WRAP'
#!/bin/sh
case "${SSH_ORIGINAL_COMMAND:-}" in
  check) printf 'decky-power:ok\n'; exit 0 ;;
  capabilities) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless capabilities ;;
  headless-status) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless status ;;
  headless-start) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless start ;;
  headless-stop) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless stop ;;
  headless-pair) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless pair ;;
  headless-pair-status) exec /usr/bin/sudo -n /usr/local/libexec/deckpilot-headless pair-status ;;
  poweroff) exec /usr/bin/sudo -n /usr/bin/systemctl --no-block poweroff ;;
  *) printf 'Command not allowed\n' >&2; exit 64 ;;
esac
WRAP
chmod 0755 /usr/local/libexec/decky-power-command
chown root:root /usr/local/libexec/decky-power-command
HOME_DIR="$(getent passwd deckpower | cut -d: -f6)"
for SAFE_PATH in "$HOME_DIR" "$HOME_DIR/.ssh" "$HOME_DIR/.ssh/authorized_keys"; do
  if [[ -L "$SAFE_PATH" ]]; then
    echo "Мастер остановлен: $SAFE_PATH является символической ссылкой." >&2; exit 1
  fi
done
install -d -m 0700 -o deckpower -g deckpower "$HOME_DIR/.ssh"
if [[ ! -e "$HOME_DIR/.ssh/authorized_keys" ]]; then
  install -o deckpower -g deckpower -m 0600 /dev/null "$HOME_DIR/.ssh/authorized_keys"
fi
chown deckpower:deckpower "$HOME_DIR/.ssh/authorized_keys"
chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
if command -v getenforce >/dev/null && [[ "$(getenforce)" != Disabled ]]; then
  if ! command -v semanage >/dev/null; then
    if command -v dnf >/dev/null; then
      dnf install -y policycoreutils-python-utils
    elif command -v apt-get >/dev/null; then
      apt-get install -y policycoreutils-python-utils
    else
      echo 'Для вашей политики SELinux нужен semanage. Установите его и повторите настройку.' >&2; exit 1
    fi
  fi
  semanage fcontext -a -t user_home_dir_t '/var/lib/decky-power' 2>/dev/null || \
    semanage fcontext -m -t user_home_dir_t '/var/lib/decky-power'
  semanage fcontext -a -t ssh_home_t '/var/lib/decky-power/.ssh(/.*)?' 2>/dev/null || \
    semanage fcontext -m -t ssh_home_t '/var/lib/decky-power/.ssh(/.*)?'
  restorecon -RF "$HOME_DIR"
fi
SUDOERS_TMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TMP"' EXIT
printf 'deckpower ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block poweroff\n' > "$SUDOERS_TMP"
chmod 0440 "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null
for ACTION in capabilities status start stop pair pair-status; do
  printf 'deckpower ALL=(root) NOPASSWD: /usr/local/libexec/deckpilot-headless %s\n' "$ACTION" >> "$SUDOERS_TMP"
done
visudo -cf "$SUDOERS_TMP" >/dev/null
install -o root -g root -m 0440 "$SUDOERS_TMP" /etc/sudoers.d/decky-power

# Keep account-specific Match directives after global sshd settings.
SSHD_BACKUP="/etc/ssh/sshd_config.deckpilot-$(date +%Y%m%d-%H%M%S).bak"
cp -p /etc/ssh/sshd_config "$SSHD_BACKUP"
OLD_INCLUDE_BACKUP=''
if [[ -f /etc/ssh/sshd_config.d/91-decky-power.conf ]] && \
   grep -qx 'Match User deckpower' /etc/ssh/sshd_config.d/91-decky-power.conf; then
  OLD_INCLUDE_BACKUP="${SSHD_BACKUP}.old-include"
  mv /etc/ssh/sshd_config.d/91-decky-power.conf "$OLD_INCLUDE_BACKUP"
fi
python3 - <<'SSHCONFIG'
from pathlib import Path
import re
p=Path('/etc/ssh/sshd_config')
s=p.read_text()
s=re.sub(r'(?ms)^# BEGIN DECKPILOT\n.*?^# END DECKPILOT\n?', '', s)
s+='\n# BEGIN DECKPILOT\nMatch User deckpower\n    ForceCommand /usr/local/libexec/decky-power-command\n    AuthenticationMethods publickey\n    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n    DisableForwarding yes\n    AllowTcpForwarding no\n    X11Forwarding no\n    PermitTTY no\n    PermitUserRC no\n# END DECKPILOT\n'
p.write_text(s)
SSHCONFIG
rollback_sshd() {
  cp -p "$SSHD_BACKUP" /etc/ssh/sshd_config
  if [[ -n "$OLD_INCLUDE_BACKUP" ]]; then
    cp -p "$OLD_INCLUDE_BACKUP" /etc/ssh/sshd_config.d/91-decky-power.conf
  fi
}
ssh-keygen -A >/dev/null
install -d -m 0755 /run/sshd
if ! sshd -t; then
  rollback_sshd
  echo 'sshd отклонил конфигурацию. Мастер восстановил прежний файл.' >&2; exit 1
fi
SSH_CONFIG="$(sshd -T -C user=deckpower,host=localhost,addr=127.0.0.1 | tr '[:upper:]' '[:lower:]')"
if ! grep -qx 'authenticationmethods publickey' <<< "$SSH_CONFIG" || \
   ! grep -qx 'passwordauthentication no' <<< "$SSH_CONFIG" || \
   ! grep -qx 'forcecommand /usr/local/libexec/decky-power-command' <<< "$SSH_CONFIG" || \
   ! grep -qx 'allowtcpforwarding no' <<< "$SSH_CONFIG" || \
   ! grep -qx 'permittty no' <<< "$SSH_CONFIG"; then
  rollback_sshd
  echo 'Правила SSH конфликтуют с настройками ПК. Мастер восстановил прежний файл.' >&2; exit 1
fi
SSH_UNIT=sshd
if [[ -f /lib/systemd/system/ssh.service || -f /usr/lib/systemd/system/ssh.service ]]; then SSH_UNIT=ssh; fi
systemctl enable --now "$SSH_UNIT"
systemctl try-restart "$SSH_UNIT"
SSH_PORT="$(sshd -T | awk 'tolower($1)=="port" && !found { print $2; found=1 }')"
[[ -n "$SSH_PORT" ]] || SSH_PORT=22

# Select a physical wired Ethernet device for Wake-on-LAN.
mapfile -t NICS < <(find /sys/class/net/ -mindepth 1 -maxdepth 1 -type l -printf '%f\n' | grep -E '^(en|eth)' | while read -r nic; do
    [[ -d "/sys/class/net/$nic/device" ]] && echo "$nic"
  done | sort || true)
NIC=''
if (( ${#NICS[@]} == 1 )); then
  NIC="${NICS[0]}"
elif (( ${#NICS[@]} > 1 )); then
  echo 'Укажи проводной интерфейс для Wake-on-LAN:'
  select choice in "${NICS[@]}" 'Пропустить'; do
    if [[ "$choice" == 'Пропустить' ]]; then break; fi
    if [[ -n "$choice" ]]; then NIC="$choice"; break; fi
  done
fi
MAC=''
if [[ -n "$NIC" ]]; then
  MAC="$(cat "/sys/class/net/$NIC/address")"
  CONNECTION=''
  if command -v nmcli >/dev/null 2>&1; then
    CONNECTION="$(nmcli -g GENERAL.CONNECTION device show "$NIC" 2>/dev/null | head -n 1 || true)"
  fi
  if [[ -n "$CONNECTION" && "$CONNECTION" != '--' ]]; then
    nmcli connection modify "$CONNECTION" 802-3-ethernet.wake-on-lan magic || true
  fi
  if ! ethtool -s "$NIC" wol g; then
    echo 'WoL не удалось активировать. Возможно, требуется настройка BIOS/UEFI.'
  fi
  echo "Ethernet: $NIC | MAC: $MAC"
  ethtool "$NIC" | grep -E 'Supports Wake-on:|Wake-on:' || true
  ETHTOOL_PATH="$(command -v ethtool)"
  cat >/etc/systemd/system/deckpilot-wol.service <<WOL
[Unit]
Description=DeckPilot Wake-on-LAN
After=network-online.target
Wants=network-online.target
ConditionPathExists=/sys/class/net/$NIC
[Service]
Type=oneshot
ExecStart=$ETHTOOL_PATH -s $NIC wol g
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
WOL
  systemctl daemon-reload
  systemctl enable deckpilot-wol.service
else
  echo 'Ethernet не выбран. MAC можно указать вручную в Decky позже.'
fi


# Embedded helpers are generated by tools/build.py.
cat >/usr/local/libexec/deckpilot-headless <<'DECKPILOT_PAYLOAD'
#!/usr/bin/python3
"""Root-owned, fixed-command bridge. Never executes the caller's shell input."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG = Path('/etc/deckpilot-headless.json')
ALLOWED = ('status', 'start', 'stop', 'pair', 'pair-status')


def run(args, payload='', timeout=300):
    return subprocess.run(args, input=payload, text=True, capture_output=True,
                          timeout=timeout, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})


def dispatch(config, action, payload=''):
    mode = config.get('mode', '')
    if action == 'capabilities':
        return {'protocol': 1, 'platform': 'linux', 'headless': mode,
                'steam_pair': mode == 'ambxst'}
    if action not in ALLOWED or mode not in ('ambxst', 'docker'):
        raise ValueError('Интеграция Headless не настроена')
    if action == 'pair' and not re.fullmatch(r'[0-9]{4}\n?', payload):
        raise ValueError('Нужен четырёхзначный PIN Steam Link')
    if mode == 'ambxst':
        user, controller, home = (config[k] for k in ('user', 'controller', 'home'))
        # Drop root before opening any user-controlled file or Docker socket.
        command = ['/usr/bin/runuser', '-u', user, '--', '/usr/bin/env',
                   'HOME=' + home, 'XDG_RUNTIME_DIR=/run/user/' + str(config['uid']),
                   'XDG_CONFIG_HOME=' + home + '/.config', 'XDG_CACHE_HOME=' + home + '/.cache',
                   'LANG=ru_RU.UTF-8', '/usr/bin/python3', controller, action]
        result = run(command, payload)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise ValueError('Контроллер Ambxst не вернул JSON. Проверьте установку мода.')
        if not isinstance(value, dict):
            raise ValueError('Неверный ответ контроллера Ambxst')
        if result.returncode or value.get('error'):
            raise ValueError(str(value.get('error') or 'Контроллер Ambxst отклонил команду')[:240])
        return value
    if action in ('pair', 'pair-status'):
        raise ValueError('PIN через Deck доступен для контроллера Ambxst; используйте рабочий стол Steam Headless')
    name = config['container']
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', name):
        raise ValueError('Неверное имя контейнера')
    docker = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock']
    if action != 'status':
        args = ['start', name] if action == 'start' else ['stop', '--time=-1', name]
        result = run(docker + args)
        if result.returncode:
            raise ValueError('Docker отклонил команду для выбранного контейнера')
    result = run(docker + ['inspect', '--format', '{{json .State}}', name], timeout=10)
    if result.returncode:
        raise ValueError('Выбранный контейнер недоступен')
    value = json.loads(result.stdout)
    return {'state': value['Status'], 'running': value['Running'],
            'ready': False, 'note': 'Docker проверен; готовность Steam проверяйте на Deck.'}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED + ('capabilities',):
        raise ValueError('Недопустимая команда')
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    payload = sys.stdin.readline(16) if sys.argv[1] == 'pair' else ''
    print(json.dumps(dispatch(config, sys.argv[1], payload), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # No raw Docker/SSH output, tokens or profile content in network errors.
        print(json.dumps({'error': 'Headless: операция не завершена. Проверьте контроллер и статус на ПК.'}))
        sys.exit(1)

DECKPILOT_PAYLOAD
chmod 0755 /usr/local/libexec/deckpilot-headless
chown root:root /usr/local/libexec/deckpilot-headless
cat >/usr/local/libexec/deckpilot-configure <<'DECKPILOT_PAYLOAD'
#!/usr/bin/python3
"""Interactive host-side setup, embedded in the standalone Linux installer."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess


def main():
    user = os.environ.get('SUDO_USER') or os.environ.get('DECKPILOT_USER', '')
    account = pwd.getpwnam(user) if user and user != 'root' else None
    candidate = None
    if account:
        home = Path(account.pw_dir)
        # Prefer the installed Ambxst package over a possibly stale CLI copy.
        for path in (home/'.local/share/ambxst/mods/packages/local.steam-headless-control/backend/steam_headless.py',
                     home/'.local/bin/steam-headless'):
            if path.is_file():
                candidate = path
                break
    print('\nSteam Headless: отдельная игровая сессия Steam в Linux-контейнере на ПК.')
    print('Она транслирует игры на Deck через Steam Remote Play / Steam Link, не занимая основной рабочий стол.')
    print('DeckPilot управляет уже установленной сессией; установка самого Steam Headless выполняется отдельно.')
    installed = input('Steam Headless уже установлен и его нужно подключить? [y/N]: ').strip().lower()
    if installed not in ('y', 'yes', 'д', 'да'):
        target = Path('/etc/deckpilot-headless.json')
        target.write_text('{}\n')
        target.chmod(0o600)
        print('Только питание ПК. Позже повторите мастер и включите Steam Headless в настройках DeckPilot.')
        return
    print('\nКак управлять Steam Headless:')
    print('1: Контроллер Ambxst' + (f' (найден у {user})' if candidate else ' (указать пользователя)'))
    print('2: Существующий контейнер Steam Headless в обычном Docker')
    print('3: Только управление питанием ПК')
    default = '1' if candidate else '3'
    choice = input(f'Выберите [{default}]: ').strip() or default
    config = {}
    if choice == '1':
        if not candidate:
            user = input('Пользователь Linux, у которого установлен контроллер: ').strip()
            account = pwd.getpwnam(user)
            if account.pw_uid == 0:
                raise ValueError('Контроллер нельзя запускать от root')
            home = Path(account.pw_dir)
            candidate = home/'.local/bin/steam-headless'
            packaged = home/'.local/share/ambxst/mods/packages/local.steam-headless-control/backend/steam_headless.py'
            if packaged.is_file():
                candidate = packaged
        if not candidate.is_file():
            raise ValueError('Контроллер не найден. Сначала установите мод Ambxst или выберите Docker.')
        probe = subprocess.run(['/usr/bin/runuser', '-u', user, '--', '/usr/bin/env',
            'HOME='+str(home), '/usr/bin/python3', str(candidate), 'config-check'],
            capture_output=True, text=True, timeout=30)
        result = json.loads(probe.stdout)
        if probe.returncode or result.get('error'):
            raise ValueError('Контроллер найден, но config-check не прошёл. Исправьте подключение в моде.')
        config = {'mode': 'ambxst', 'user': user, 'home': str(home),
                  'uid': account.pw_uid, 'controller': str(candidate)}
    elif choice == '2':
        print('Будет использоваться только /var/run/docker.sock. Docker не устанавливается.')
        probe = subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock',
                                'ps', '-a', '--format', '{{.Names}}'],
                               capture_output=True, text=True, timeout=10, check=True)
        names = probe.stdout.splitlines()
        print('Контейнеры: ' + ', '.join(names))
        name = input('Имя существующего контейнера Steam Headless [steam-headless]: ').strip() or 'steam-headless'
        if name not in names or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', name):
            raise ValueError('Выберите существующий контейнер по точному имени')
        print('Остановка отправляет контейнеру SIGTERM и ждёт без принудительного SIGKILL.')
        config = {'mode': 'docker', 'container': name}
    elif choice != '3':
        raise ValueError('Выберите 1, 2 или 3')
    target = Path('/etc/deckpilot-headless.json')
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n')
    target.chmod(0o600)
    print('Headless: ' + (config.get('mode') or 'не используется'))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit('Ошибка настройки Headless: ' + str(error))

DECKPILOT_PAYLOAD
chmod 0755 /usr/local/libexec/deckpilot-configure
chown root:root /usr/local/libexec/deckpilot-configure
cat >/usr/local/libexec/decky-power-pair <<'DECKPILOT_PAYLOAD'
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

DECKPILOT_PAYLOAD
chmod 0755 /usr/local/libexec/decky-power-pair
chown root:root /usr/local/libexec/decky-power-pair

python3 /usr/local/libexec/deckpilot-configure
PAIR_UFW_RULES=()
cleanup_pairing() {
  for CIDR in "${PAIR_UFW_RULES[@]}"; do
    ufw --force delete allow proto tcp from "$CIDR" to any port 47833 >/dev/null || true
  done
  rm -f "$SUDOERS_TMP"
}
trap cleanup_pairing EXIT
mapfile -t LAN_CIDRS < <(python3 - <<'LANPY'
import ipaddress,json,subprocess
from pathlib import Path
private=[ipaddress.ip_network(c) for c in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
for iface in json.loads(subprocess.check_output(['ip','-j','-4','addr','show','up'])):
    if not (Path('/sys/class/net')/iface['ifname']/'device').exists():continue
    for addr in iface.get('addr_info',[]):
        net=ipaddress.ip_network(f"{addr['local']}/{addr['prefixlen']}",strict=False)
        if any(net.subnet_of(p) for p in private):print(net)
LANPY
)
if command -v ufw >/dev/null && LC_ALL=C ufw status | grep -q '^Status: active'; then
  read -r -p 'Разрешить связь с Deck в UFW для локальной сети? [Y/n]: ' FIREWALL_REPLY
  if [[ "$FIREWALL_REPLY" != [nNнН] ]]; then
    for CIDR in "${LAN_CIDRS[@]}"; do
      ufw allow proto tcp from "$CIDR" to any port "$SSH_PORT" comment 'DeckPilot SSH'
      if ! LC_ALL=C ufw status | grep -F "$CIDR" | grep -q 47833; then
        ufw allow proto tcp from "$CIDR" to any port 47833 comment 'DeckPilot temporary pairing'
        PAIR_UFW_RULES+=("$CIDR")
      fi
    done
  fi
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  read -r -p 'Разрешить связь с Deck в firewalld для локальной сети? [Y/n]: ' FIREWALL_REPLY
  if [[ "$FIREWALL_REPLY" != [nNнН] ]]; then
    for CIDR in "${LAN_CIDRS[@]}"; do
      RULE="rule family=ipv4 source address=$CIDR port port=$SSH_PORT protocol=tcp accept"
      firewall-cmd --add-rich-rule="$RULE"
      firewall-cmd --permanent --add-rich-rule="$RULE"
      firewall-cmd --add-rich-rule="rule family=ipv4 source address=$CIDR port port=47833 protocol=tcp accept" --timeout=300
    done
  fi
fi
echo
echo 'IP-адреса ПК:'
ip -4 -br addr show scope global
echo 'На Deck: Decky → DeckPilot → Подключить ПК.'
echo 'После ввода PIN сверьте короткий код на обоих устройствах.'
echo 'Если поиск не работает, проверьте, что Deck и ПК находятся в одной локальной сети.'
runuser -u deckpower -- env DECKY_POWER_MAC="$MAC" DECKY_POWER_SSH_PORT="$SSH_PORT" /usr/bin/python3 -u /usr/local/libexec/decky-power-pair
