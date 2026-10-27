#!/usr/bin/env python3
"""Rebuild standalone installer and zero-dependency Decky frontend."""
from pathlib import Path
import shutil
import json
import re
import ast

ROOT = Path(__file__).resolve().parent.parent


def build():
    tools = ROOT/'tools'
    script = (tools/'setup-linux.template.sh').read_text()
    for source, destination in (
        ('headless_bridge.py', '/usr/local/libexec/deckpilot-headless'),
        ('configure_headless.py', '/usr/local/libexec/deckpilot-configure'),
        ('pair_server.py', '/usr/local/libexec/decky-power-pair'),
    ):
        script += f"cat >{destination} <<'DECKPILOT_PAYLOAD'\n"
        script += (tools/source).read_text() + '\nDECKPILOT_PAYLOAD\n'
        script += f'chmod 0755 {destination}\nchown root:root {destination}\n'
    script += '''
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
'''
    (tools/'setup-linux.sh').write_text(script)
    (tools/'setup-linux.sh').chmod(0o755)
    # Keep setup-arch.sh as a compatibility alias.
    shutil.copy2(tools/'setup-linux.sh', tools/'setup-arch.sh')
    catalogs = {language: json.loads((ROOT/'locales'/f'{language}.json').read_text())
                for language in ('ru', 'en')}
    if catalogs['ru'].keys() != catalogs['en'].keys():
        raise ValueError('Locale catalogs must contain the same messages')
    for key, russian in catalogs['ru'].items():
        english = catalogs['en'][key]
        if not russian or not english:
            raise ValueError(f'Empty translation: {key}')
        if sorted(re.findall(r'\{\w+\}|%s', russian)) != sorted(re.findall(r'\{\w+\}|%s', english)):
            raise ValueError(f'Translation placeholders differ: {key}')
    frontend = (ROOT/'src/index.js').read_text()
    required = {ast.literal_eval(match.group(1)) for match in re.finditer(
                r"\bt\(('(?:\\.|[^'\\])*')", frontend)}
    required.update(node.value for node in ast.walk(ast.parse((ROOT/'main.py').read_text()))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.search('[А-Яа-яЁё]', node.value))
    missing = required - catalogs['ru'].keys()
    if missing:
        raise ValueError(f'Missing locale messages: {sorted(missing)}')
    i18n = (ROOT/'src/i18n.js').read_text().replace('__DECKPILOT_MESSAGES__',
                json.dumps(catalogs, ensure_ascii=False, separators=(',', ':')))
    source = frontend.replace(
        "import {translate, translateError} from './i18n.js';", i18n)
    (ROOT/'dist/index.js').write_text(source)


if __name__ == '__main__':
    build()
