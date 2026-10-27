"""DeckPilot: PC power and optional Steam Headless control for Decky Loader.

Derived from proilyxa/deck-pc-on-off (BSD-3-Clause).
Runs as the normal 'deck' user (no root privileges).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import http.client
import time
import json
import os
import re
import socket
import subprocess
import tempfile
import uuid
import hashlib
from pathlib import Path

import decky

LOCAL_NETS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"
))
MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,90}$")
PAIR_PORT = 47833


def verification_code(host_fp, deck_fp):
    digest = hashlib.sha256((host_fp + '|' + deck_fp).encode('ascii')).hexdigest()[:12].upper()
    return '-'.join(digest[i:i+4] for i in range(0, 12, 4))


def _ssh_request(key, host, command, payload='', timeout=15):
    allowed = {'check', 'capabilities', 'headless-status', 'headless-start',
               'headless-stop', 'headless-pair', 'headless-pair-status'}
    if command not in allowed:
        raise ValueError('Недопустимая команда')
    known = Path.home() / '.ssh' / 'known_hosts'
    args = ['/usr/bin/ssh', '-F', '/dev/null', '-i', str(key), '-p', str(host['ssh_port']),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
            '-o', 'KbdInteractiveAuthentication=no', '-o', 'ConnectTimeout=5',
            '-o', 'ConnectionAttempts=1', '-o', 'StrictHostKeyChecking=yes',
            '-o', f'UserKnownHostsFile={known}', '-o', 'GlobalKnownHostsFile=/dev/null',
            '--', 'deckpower@' + local_ip(host['ip']), command]
    try:
        done = subprocess.run(args, input=payload, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('ПК не ответил вовремя. Обновите статус: операция могла продолжиться на ПК.') from exc
    if done.returncode:
        # Controller errors are JSON; never expose arbitrary SSH stderr.
        try:
            error = json.loads(done.stdout).get('error')
        except (ValueError, AttributeError):
            error = None
        raise ValueError(str(error)[:240] if error else 'SSH-команда не выполнена. Проверьте сопряжение и доступность ПК.')
    return done.stdout.strip()


def _capabilities(key, host):
    try:
        data = json.loads(_ssh_request(key, host, 'capabilities'))
        if not isinstance(data, dict) or data.get('protocol') != 1:
            raise ValueError()
        return {'platform': str(data.get('platform', 'linux'))[:24],
                'headless': data.get('headless') in ('ambxst', 'docker') and data['headless'] or '',
                'steam_pair': data.get('steam_pair') is True}
    except (ValueError, OSError, subprocess.SubprocessError):
        # Legacy helpers support check/poweroff.
        return {'platform': 'unknown', 'headless': '', 'steam_pair': False}


def local_ip(value: str) -> str:
    """Validate a private RFC1918 IPv4 address."""
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except (ipaddress.AddressValueError, AttributeError) as exc:
        raise ValueError("Укажите IPv4-адрес компьютера в локальной сети") from exc
    if not any(ip in network for network in LOCAL_NETS):
        raise ValueError("Доступны только адреса частной локальной сети (IPv4)")
    return str(ip)


def normalize_mac(raw: str) -> str:
    value = re.sub(r"[.:-]", "", (raw or "").strip())
    if not MAC_RE.fullmatch(value):
        raise ValueError("MAC должен содержать 12 шестнадцатеричных символов")
    values = bytes.fromhex(value)
    if values in (b"\x00" * 6, b"\xff" * 6) or values[0] & 1:
        raise ValueError("Неверный MAC-адрес сетевой карты")
    return ":".join(f"{x:02x}" for x in values)


def int_port(value) -> int:
    try:
        port = int(value)
        if isinstance(value, bool) or not (1 <= port <= 65535):
            raise ValueError()
        return port
    except (TypeError, ValueError) as exc:
        raise ValueError("Порт SSH должен быть числом от 1 до 65535") from exc


def _neighbors():
    """Read LAN neighbors with iproute2."""
    try:
        proc = subprocess.run(["/usr/bin/ip", "-j", "-4", "neigh", "show"],
                              capture_output=True, text=True, timeout=3, check=True)
        entries = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    result = {}
    for entry in entries:
        state = entry.get("state") or ""
        states = state if isinstance(state, list) else [state]
        if any(str(s).upper() in ("FAILED", "INCOMPLETE") for s in states):
            continue
        try:
            addr = local_ip(entry["dst"])
            result[addr] = normalize_mac(entry["lladdr"])
        except (KeyError, ValueError, TypeError):
            pass
    return result


def _interfaces():
    """Connected IPv4 LAN interfaces, excluding bridges, VPN and docker."""
    try:
        proc = subprocess.run(["/usr/bin/ip", "-j", "-4", "addr", "show", "up"],
                              capture_output=True, text=True, timeout=3, check=True)
        interfaces = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    result = []
    for iface in interfaces:
        name = iface.get("ifname", "")
        if name == "lo" or name.startswith(("docker", "br-", "veth", "tun", "tap", "wg", "virbr", "tailscale")):
            continue
        for item in iface.get("addr_info", []):
            addr = item.get("local", "")
            try:
                addr = local_ip(addr)
                prefix = int(item.get("prefixlen", 24))
                result.append((addr, ipaddress.IPv4Network(f"{addr}/{prefix}", strict=False)))
            except (ValueError, TypeError):
                continue
    return result


def _ssh_open(ip: str, port: int, timeout=0.45):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _discover():
    """Find SSH ports and ARP neighbors within the Deck's /24."""
    addresses = set()
    for local, network in _interfaces():
        # Even on a /16 LAN, limit one scan to the Deck's current /24.
        block = network if network.prefixlen >= 24 else ipaddress.IPv4Network(f"{local}/24", strict=False)
        if block.num_addresses > 256:
            continue
        addresses.update(str(addr) for addr in block.hosts() if str(addr) != local)
    # Don't scan if we cannot identify a private physical LAN.
    if not addresses:
        return []
    neighbors = _neighbors()
    active = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        checks = {pool.submit(_ssh_open, addr, 22, 0.30): addr for addr in sorted(addresses)}
        for future in concurrent.futures.as_completed(checks):
            if future.result():
                active.append(checks[future])
    neighbors.update(_neighbors())  # Refresh ARP after probing.
    for addr in neighbors:
        if addr in addresses and addr not in active:
            active.append(addr)
    return [{"ip": addr, "mac": neighbors.get(addr, ""), "name": addr}
            for addr in sorted(set(active), key=ipaddress.IPv4Address)][:64]



def _pair_info(ip: str):
    """Read the pairing info from a private IPv4 address."""
    ip = local_ip(ip)
    conn = http.client.HTTPConnection(ip, PAIR_PORT, timeout=2.5)
    try:
        conn.request("GET", "/info", headers={"Accept":"application/json"})
        response = conn.getresponse()
        data = response.read(2048)
        if response.status != 200:
            raise ValueError("На ПК не запущен мастер сопряжения")
        info = json.loads(data)
        if info.get("protocol") != 1 or not FINGERPRINT_RE.fullmatch(info.get("fingerprint", "")):
            raise ValueError("Ответ мастера на ПК некорректен")
        mac = info.get("mac", "")
        if mac:
            mac = normalize_mac(mac)
        return {"ip":ip, "name":str(info.get("name", "ПК"))[:48],
                "mac":mac, "ssh_port":int_port(info.get("ssh_port",22)),
                "fingerprint":info["fingerprint"]}
    except (OSError, TimeoutError) as exc:
        raise ValueError("Нет связи с мастером на ПК: проверьте IP и локальную сеть") from exc
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Мастер на ПК вернул неправильные данные") from exc
    finally:
        conn.close()


def _ssh_fingerprint(key_text):
    result = subprocess.run(['/usr/bin/ssh-keygen','-lf','-'],input=key_text+'\n',
                            text=True,capture_output=True,timeout=4)
    if result.returncode:
        raise ValueError("Некорректный ключ SSH")
    fields = result.stdout.split()
    if len(fields) < 2 or not FINGERPRINT_RE.fullmatch(fields[1]):
        raise ValueError("Не удалось вычислить отпечаток SSH")
    return fields[1]


def _scan_host_key(ip, port):
    cmd = ['/usr/bin/ssh-keyscan','-T','3','-p',str(port),'-t','ed25519',local_ip(ip)]
    response = subprocess.run(cmd,text=True,capture_output=True,timeout=6)
    for line in response.stdout.splitlines():
        if line.startswith('#'):
            continue
        fields = line.strip().split()
        if len(fields) >= 3 and fields[1] == 'ssh-ed25519':
            return line.strip(), _ssh_fingerprint(fields[1]+' '+fields[2])
    raise ValueError("Не удалось прочитать ED25519 SSH-ключ компьютера")


def _private_pubkey(key_path: Path):
    folder = key_path.parent
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    folder.chmod(0o700)
    pub = Path(str(key_path)+'.pub')
    if not key_path.is_file() and not pub.exists():
        proc = subprocess.run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','',
                               '-f',str(key_path),'-C','decky-power'],
                              capture_output=True,text=True,timeout=12)
        if proc.returncode:
            raise ValueError("Не удалось создать отдельный SSH-ключ на Deck")
    if not key_path.is_file() or not pub.is_file():
        raise ValueError("Некорректная пара ключей decky-power на Deck")
    if key_path.is_symlink() or pub.is_symlink():
        raise ValueError("SSH-ключи не должны быть символическими ссылками")
    key_path.chmod(0o600)
    value = pub.read_text(encoding='utf-8').strip()
    fields = value.split()
    if len(fields) < 2 or fields[0] != 'ssh-ed25519':
        raise ValueError("Ожидался публичный ключ ed25519")
    key = fields[0]+' '+fields[1]
    return key, _ssh_fingerprint(key)


def _pair_submit(ip, code, key, expected_fingerprint):
    # The client sends the PIN over LAN HTTP. The user compares verification
    # codes on both devices before approving the key on the PC.
    payload = json.dumps({'code':code,'public_key':key},separators=(',',':')).encode()
    conn = http.client.HTTPConnection(ip, PAIR_PORT, timeout=68)
    try:
        conn.request('POST','/pair',body=payload,
                     headers={'Content-Type':'application/json','Cache-Control':'no-store'})
        response = conn.getresponse()
        raw = response.read(2048)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Неверный ответ мастера на ПК")
        if response.status != 200 or not data.get('success'):
            if response.status == 403:
                raise ValueError("Код неверен или подключение не подтверждено на ПК")
            if response.status == 409:
                raise ValueError("На ПК уже ожидает подтверждения другой запрос")
            if response.status == 429:
                raise ValueError("Слишком много попыток. Перезапустите мастер на ПК")
            raise ValueError("Сопряжение не выполнено: проверьте мастер на ПК")
        if data.get('fingerprint') != expected_fingerprint:
            raise ValueError("Отпечаток SSH на ПК изменился во время сопряжения")
        return data
    except (OSError, TimeoutError) as exc:
        raise ValueError("Истекло время ожидания: проверьте подтверждение YES на ПК") from exc
    finally:
        conn.close()


def _try_ssh_check(key, host, known_hosts):
    cmd = ['/usr/bin/ssh','-F','/dev/null','-i',str(key),'-p',str(host['ssh_port']),
           '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
           '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no',
           '-o','ConnectionAttempts=1','-o','ConnectTimeout=5',
           '-o','StrictHostKeyChecking=yes',
           '-o',f'UserKnownHostsFile={known_hosts}',
           '-o','GlobalKnownHostsFile=/dev/null','--',
           'deckpower@'+host['ip'],'check']
    done = subprocess.run(cmd,capture_output=True,text=True,timeout=10)
    if done.returncode != 0 or done.stdout.strip() != 'decky-power:ok':
        if 'permission denied' in done.stderr.lower():
            raise ValueError('SSH отказал в доступе. Проверьте завершение сопряжения на ПК')
        raise ValueError('Не прошла проверка SSH. Проверьте настройки sshd на ПК')


def _store_known_host(known_hosts: Path, line: str, ip: str, port: int):
    known_hosts.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    known_hosts.parent.chmod(0o700)
    if known_hosts.is_symlink():
        raise ValueError('known_hosts не должен быть символической ссылкой')
    if known_hosts.exists():
        existing = subprocess.run(['/usr/bin/ssh-keygen','-F',
                                   f'[{ip}]:{port}' if port != 22 else ip,
                                   '-f',str(known_hosts)], capture_output=True, text=True,timeout=4)
        if existing.returncode == 0:
            # Do not overwrite a previously trusted different key.
            if line.split()[1:] != [part for old in existing.stdout.splitlines() if not old.startswith('#')
                                    for part in old.split()[1:]][:2]:
                raise ValueError('Для этого IP уже сохранён другой SSH-ключ. Разрешите конфликт вручную.')
            return
    with known_hosts.open('a',encoding='utf-8') as out:
        out.write(line+'\n')
    known_hosts.chmod(0o600)


def _discover_pairing():
    addresses = set()
    for local, network in _interfaces():
        block = network if network.prefixlen >= 24 else ipaddress.IPv4Network(f'{local}/24',strict=False)
        if block.num_addresses <= 256:
            addresses.update(str(addr) for addr in block.hosts() if str(addr) != local)
    if not addresses:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=56) as pool:
        futures = {pool.submit(_ssh_open,ip,PAIR_PORT,0.27):ip for ip in addresses}
        found = [futures[f] for f in concurrent.futures.as_completed(futures) if f.result()]
    matches=[]
    for ip in sorted(found,key=ipaddress.IPv4Address):
        try:
            matches.append(_pair_info(ip))
        except ValueError:
            continue
    return matches[:24]


class Plugin:
    def __init__(self):
        self.store = Path(decky.DECKY_PLUGIN_SETTINGS_DIR) / "hosts.json"
        self.key = Path.home() / ".ssh" / "decky-power_ed25519"
        self.hosts = []

    def _load(self):
        try:
            raw = json.loads(self.store.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("invalid hosts list")
            cleaned = []
            for host in raw[:100]:
                try:
                    ip = local_ip(host.get("ip", ""))
                    mac = host.get("mac", "")
                    try:
                        mac = normalize_mac(mac)
                    except ValueError:
                        mac = ""
                    cleaned.append({"id": str(host.get("id") or uuid.uuid4()),
                                    "name": str(host.get("name") or ip)[:48],
                                    "ip": ip, "mac": mac,
                                    # Original plugin's HTTP port 9876 is NOT an SSH port.
                                    "ssh_port": int_port(host.get("ssh_port", 22)),
                                    "platform": str(host.get('platform', 'unknown'))[:24],
                                    "headless": host.get('headless') if host.get('headless') in ('ambxst','docker') else '',
                                    "steam_pair": host.get('steam_pair') is True,
                                    "show_headless": host.get('show_headless') is True})
                except (ValueError, TypeError, AttributeError):
                    continue
            self.hosts = cleaned
        except FileNotFoundError:
            self.hosts = []
        except (OSError, ValueError, TypeError) as exc:
            decky.logger.warning(f"Could not read hosts: {exc}")
            self.hosts = []

    def _save(self):
        self.store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.store.parent, prefix=".hosts-",
                                             encoding="utf-8", delete=False) as tmp:
                name = tmp.name
                os.fchmod(tmp.fileno(), 0o600)
                json.dump(self.hosts, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(name, self.store)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def _get(self, host_id):
        self._load()
        return next((h for h in self.hosts if h["id"] == str(host_id)), None)

    async def get_hosts(self):
        self._load()
        return self.hosts

    async def get_preferences(self):
        try:
            value = json.loads(self.store.with_name('preferences.json').read_text(encoding='utf-8'))
            language = value.get('language') if isinstance(value, dict) else None
        except (OSError, ValueError):
            language = None
        return {'language': language if language in ('ru', 'en') else 'ru'}

    async def set_language(self, language: str):
        if language not in ('ru', 'en'):
            return {'success': False, 'error': 'Выберите русский или английский язык'}
        target = self.store.with_name('preferences.json')
        temporary = None
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=target.parent,
                    prefix='.preferences-', encoding='utf-8', delete=False) as out:
                temporary = out.name
                os.fchmod(out.fileno(), 0o600)
                json.dump({'language': language}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            return {'success': True, 'language': language}
        except OSError:
            return {'success': False, 'error': 'Не удалось сохранить язык. Проверьте доступ к настройкам DeckPilot.'}
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    async def import_legacy(self):
        self._load()
        if self.hosts:
            return {'success': False, 'error': 'Импорт доступен только при пустом списке ПК'}
        previous = self.store.parent.parent / 'PC On Off' / 'hosts.json'
        if not previous.is_file():
            return {'success': False, 'error': 'Настройки PC On/Off не найдены. Пройдите подключение ПК.'}
        current = self.store
        try:
            self.store = previous
            self._load()
        finally:
            self.store = current
        if not self.hosts:
            return {'success': False, 'error': 'В PC On/Off нет подходящих настроек'}
        self._save()
        return {'success': True, 'count': len(self.hosts)}

    async def discover_hosts(self):
        try:
            return {"success": True, "hosts": await asyncio.to_thread(_discover)}
        except Exception as exc:
            decky.logger.error(f"LAN scan error: {exc}")
            return {"success": False, "error": "Не удалось просканировать локальную сеть"}

    async def pairing_identity(self):
        try:
            pub, fp = await asyncio.to_thread(_private_pubkey, self.key)
            return {"success":True, "fingerprint":fp}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"success":False,"error":str(exc)}

    async def discover_pairing_hosts(self):
        try:
            return {"success":True,"hosts":await asyncio.to_thread(_discover_pairing)}
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            decky.logger.warning(f'Pairing discovery failed: {exc}')
            return {"success":False,"error":"Не удалось найти мастер на ПК"}

    async def pairing_info(self, ip):
        try:
            info = await asyncio.to_thread(_pair_info,ip)
            _, fp = await asyncio.to_thread(_private_pubkey, self.key)
            info['verification'] = verification_code(info['fingerprint'], fp)
            return {"success":True, "host":info}
        except (ValueError, OSError) as exc:
            return {"success":False,"error":str(exc)}

    def _pair_sync(self, ip, code, expected_fingerprint, enable_headless=False):
        ip = local_ip(ip)
        if not isinstance(code,str) or not re.fullmatch(r'\d{8}',code):
            raise ValueError('Введите 8-значный код, показанный на ПК')
        key, deck_fp = _private_pubkey(self.key)
        info = _pair_info(ip)
        if info['fingerprint'] != expected_fingerprint:
            raise ValueError('ПК изменился после проверки. Найдите его заново и сверьте код.')
        # Check the SSH daemon's actual host key before authorizing a Deck key.
        initial_line, initial_fp = _scan_host_key(ip,info['ssh_port'])
        if initial_fp != info['fingerprint']:
            raise ValueError('Отпечаток SSH сервера не совпадает с отпечатком мастера на ПК')
        result = _pair_submit(ip,code,key,info['fingerprint'])
        line, fp = _scan_host_key(ip,info['ssh_port'])
        if fp != initial_fp or line.split()[1:] != initial_line.split()[1:]:
            raise ValueError('SSH-ключ компьютера изменился во время сопряжения')
        host = {**info, 'mac':normalize_mac(result['mac']) if result.get('mac') else info['mac']}
        known = Path.home()/'.ssh'/'known_hosts'
        _store_known_host(known,line,ip,host['ssh_port'])
        _try_ssh_check(self.key,host,known)
        self._load()
        old = next((x for x in self.hosts if x['ip']==ip),None)
        entry = {'id':old['id'] if old else str(uuid.uuid4()),
                 'name':host['name'] or 'Игровой ПК', 'ip':ip,
                 'mac':host['mac'], 'ssh_port':host['ssh_port']}
        entry.update(_capabilities(self.key, host))
        entry['show_headless'] = enable_headless is True and bool(entry['headless'])
        if old:
            self.hosts[self.hosts.index(old)] = entry
        elif len(self.hosts) < 32:
            self.hosts.append(entry)
        else:
            raise ValueError('Максимум 32 компьютера')
        self._save()
        return {'success':True, 'host':entry, 'deck_fingerprint':deck_fp}

    async def pair_host(self, ip: str, code: str, expected_fingerprint: str, enable_headless=False):
        try:
            return await asyncio.to_thread(self._pair_sync,ip,code,expected_fingerprint,enable_headless)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"success":False,"error":str(exc)}

    async def save_host(self, host_id: str, name: str, ip: str, mac: str, ssh_port=22):
        try:
            ip = local_ip(ip)
            name = (name or "").strip()
            if not name or len(name) > 48:
                raise ValueError("Имя ПК должно содержать от 1 до 48 символов")
            port = int_port(ssh_port)
            if mac and mac.strip():
                mac = normalize_mac(mac)
            else:
                mac = await asyncio.to_thread(lambda: _neighbors().get(ip, ""))
            self._load()
            old = next((h for h in self.hosts if h["id"] == str(host_id)), None) if host_id else None
            new = {"id": old["id"] if old else str(uuid.uuid4()), "name": name,
                   "ip": ip, "mac": mac, "ssh_port": port}
            if old:
                new.update({k: old[k] for k in ('platform', 'headless', 'steam_pair', 'show_headless') if k in old})
            if old:
                self.hosts[self.hosts.index(old)] = new
            elif len(self.hosts) < 32:
                self.hosts.append(new)
            else:
                raise ValueError("Максимум 32 компьютера")
            self._save()
            return {"success": True, "host": new}
        except (OSError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    async def delete_host(self, host_id: str):
        self._load()
        initial = len(self.hosts)
        self.hosts = [host for host in self.hosts if host["id"] != str(host_id)]
        if len(self.hosts) == initial:
            return {"success": False, "error": "ПК не найден"}
        self._save()
        return {"success": True}

    async def host_status(self, host_id: str):
        host = self._get(host_id)
        if not host:
            return {"success": False, "error": "ПК не найден"}
        online = await asyncio.to_thread(_ssh_open, host["ip"], host["ssh_port"], 0.8)
        return {"success": True, "ssh_available": online}

    async def test_connection(self, host_id: str):
        host = self._get(host_id)
        if not host:
            return {'success': False, 'error': 'ПК не найден'}
        try:
            result = await asyncio.to_thread(_ssh_request, self.key, host, 'check')
            if result != 'decky-power:ok':
                raise ValueError('На ПК не установлен помощник DeckPilot')
            caps = await asyncio.to_thread(_capabilities, self.key, host)
            host.update(caps)
            self._save()
            return {'success': True, 'host': host, 'message': 'SSH и ключи проверены'}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return {'success': False, 'error': str(exc)}

    async def set_headless_visibility(self, host_id: str, enabled: bool):
        if enabled is True:
            checked = await self.test_connection(host_id)
            if not checked['success']:
                return checked
            if not checked['host'].get('headless'):
                return {'success': False, 'error': 'Сначала настройте Steam Headless на ПК и повторно запустите мастер DeckPilot на ПК, выбрав интеграцию.'}
        host = self._get(host_id)
        if not host:
            return {'success': False, 'error': 'ПК не найден'}
        host['show_headless'] = enabled is True
        self._save()
        return {'success': True, 'host': host}

    async def headless_action(self, host_id: str, action: str, pin=''):
        if action not in ('status', 'start', 'stop', 'pair', 'pair-status'):
            return {'success': False, 'error': 'Недопустимое действие Headless'}
        host = self._get(host_id)
        if not host or not host.get('headless'):
            return {'success': False, 'error': 'Headless не настроен. Запустите мастер на ПК и проверку подключения.'}
        if action == 'pair' and (not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4}', pin)):
            return {'success': False, 'error': 'Введите 4 цифры PIN Steam Link'}
        try:
            raw = await asyncio.to_thread(_ssh_request, self.key, host, 'headless-' + action,
                                         pin + '\n' if action == 'pair' else '',
                                         310 if action in ('start', 'stop') else 20)
            data = json.loads(raw)
            if not isinstance(data, dict) or len(raw) > 65536:
                raise ValueError('Некорректный ответ Headless')
            if data.get('error'):
                raise ValueError(str(data['error'])[:240])
            return {'success': True, 'data': data}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return {'success': False, 'error': str(exc)}

    async def wake_host(self, host_id: str):
        host = self._get(host_id)
        if not host:
            return {"success": False, "error": "ПК не найден"}
        try:
            mac = normalize_mac(host.get("mac", ""))
            data = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
            destinations = {"255.255.255.255"}
            for _, subnet in await asyncio.to_thread(_interfaces):
                if ipaddress.IPv4Address(host["ip"]) in subnet:
                    destinations.add(str(subnet.broadcast_address))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for _ in range(3):
                    for destination in destinations:
                        sock.sendto(data, (destination, 9))
            return {"success": True, "message": "WoL-пакеты отправлены"}
        except (ValueError, OSError) as exc:
            return {"success": False, "error": "Для WoL нужен корректный MAC. " + str(exc)}

    def _shutdown_sync(self, host):
        if not self.key.is_file():
            return {"success": False, "error": "На Deck не найден SSH-ключ ~/.ssh/decky-power_ed25519"}
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        if not known_hosts.is_file():
            return {"success": False, "error": "На Deck не настроен known_hosts: проверьте SSH-ключ сервера"}
        cmd = ["/usr/bin/ssh", "-F", "/dev/null", "-i", str(self.key),
               "-p", str(host["ssh_port"]), "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no",
               "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=5",
               "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={known_hosts}",
               "-o", "GlobalKnownHostsFile=/dev/null", "--",
               "deckpower@" + host["ip"], "poweroff"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=9)
        except FileNotFoundError:
            return {"success": False, "error": "На Deck отсутствует /usr/bin/ssh"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Истекло время ожидания SSH (возможно, ПК выключается)"}
        if done.returncode == 0:
            return {"success": True, "message": "Команда выключения принята"}
        # Never expose raw stderr unconditionally (may contain host-specific info).
        error = (done.stderr or "").lower()
        if "host key verification failed" in error:
            detail = "Не подтверждён SSH-ключ хоста (known_hosts)"
        elif "permission denied" in error:
            detail = "SSH отказал в доступе: проверьте публичный ключ на ПК"
        elif "connection refused" in error or "no route to host" in error:
            detail = "SSH на ПК недоступен"
        elif "sudo" in error:
            detail = "На ПК не настроено разрешение на выключение"
        else:
            detail = "Не удалось подтвердить выключение (код SSH: %s)" % done.returncode
        return {"success": False, "error": detail}

    async def shutdown_host(self, host_id: str):
        host = self._get(host_id)
        if not host:
            return {"success": False, "error": "ПК не найден"}
        return await asyncio.to_thread(self._shutdown_sync, host)

    async def _main(self):
        self._load()
        decky.logger.info("DeckPilot loaded")

    async def _unload(self):
        pass

    async def _uninstall(self):
        pass
