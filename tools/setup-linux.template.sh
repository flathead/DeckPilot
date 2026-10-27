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
