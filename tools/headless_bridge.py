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
