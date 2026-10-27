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
