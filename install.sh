#!/bin/bash
# OpenFile installer. Run on the DevKit, from the ability folder:
#
#   sudo ./install.sh                  volume, sync watcher, and a guest network share that shows up
#                                      by itself under Network on macOS, Windows and Linux
#   sudo ./install.sh --web            also serve the web page on port 8088
#   sudo ./install.sh --set-password   choose a password for the share. Guest access turns off
#                                      (non-interactive: OPENFILE_SHARE_PASSWORD=... in the environment)
#   sudo ./install.sh --no-password    back to no password, guest access on
#   sudo ./install.sh --dev-shares     also share live abilities and the account home (needs a password)
#   sudo ./install.sh --ethernet       switch on the wired port, which the DevKit firmware ships switched off
#   sudo ./install.sh --uninstall      remove services and shares; the volume image is kept
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOUNT="${OPENFILE_MOUNT:-/mnt/openfile}"
DEFAULTS=/etc/default/openfile
SMB_MAIN=/etc/samba/smb.conf
SMB_INCLUDE=/etc/samba/openfile.conf
SMB_DEV_INCLUDE=/etc/samba/openfile-dev.conf
UNITS=(openfile-gadget openfile-sync openfile-web)

GUEST=""; DEV_SHARES=no; UNINSTALL=no; WEB=no; LOGIN_ONLY=no; ETHERNET=no
NM_ETHERNET=/etc/NetworkManager/conf.d/20-openfile-ethernet.conf
AVAHI_SERVICE=/etc/avahi/services/openfile.service
# Out of the box the share opens with this, and guests are let in too. It is
# printed by the installer and written in the README on purpose: a published
# default is no weaker than guest access, and it gives Macs and Windows a
# named login, which both need. `openfile password` replaces it.
DEFAULT_SHARE_PASSWORD="admin"
STREAMS=/var/lib/openhome/openfile-streams
for arg in "$@"; do
  case "$arg" in
    --set-password|--require-login) GUEST=no; LOGIN_ONLY=maybe ;;
    --no-password)   GUEST=yes; LOGIN_ONLY=maybe ;;
    --dev-shares)    DEV_SHARES=yes ;;
    --web)           WEB=yes ;;
    --ethernet)      ETHERNET=yes ;;
    --uninstall)     UNINSTALL=yes ;;
    -h|--help)       sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$LOGIN_ONLY" = maybe ] && [ "$#" -eq 1 ] && [ -f "$DEFAULTS" ]; then
  LOGIN_ONLY=yes   # OpenFile is installed and only the login was asked about
else
  LOGIN_ONLY=no
fi

if [ "$EUID" -ne 0 ]; then
  echo "Run this with sudo." >&2
  exit 1
fi

# The account that owns the ability folder is the DevKit account.
DEVICE_USER="$(stat -c %U "$INSTALL_DIR")"
DEVICE_HOME="$(getent passwd "$DEVICE_USER" | cut -d: -f6)"
DEVICE_UID="$(id -u "$DEVICE_USER")"
CAPS_DIR="$(dirname "$INSTALL_DIR")"

# The share's login. The DevKit account is the share's account. Samba keeps
# its own passwords: nothing here touches the account's real password, so it
# changes nothing about SSH or the desktop login.
login_mode() {
  sed -n 's/^OPENFILE_LOGIN=//p' "$DEFAULTS" 2>/dev/null
}

set_share_password() {
  # $1 = password. Adds the Samba entry if the account has none yet.
  if pdbedit -L 2>/dev/null | grep -q "^$DEVICE_USER:"; then
    printf '%s\n%s\n' "$1" "$1" | smbpasswd -s "$DEVICE_USER" >/dev/null
  else
    printf '%s\n%s\n' "$1" "$1" | smbpasswd -s -a "$DEVICE_USER" >/dev/null
  fi
}

configure_login() {
  LOGIN="$(login_mode)"
  [ -n "$LOGIN" ] || LOGIN=default
  if [ -z "$GUEST" ]; then
    # This run did not ask about the login. Keep what the owner chose before.
    [ "$LOGIN" = custom ] && GUEST=no || GUEST=yes
  fi

  if [ "$GUEST" = no ] && [ "$LOGIN" != custom ]; then
    if [ -n "${OPENFILE_SHARE_PASSWORD:-}" ]; then
      set_share_password "$OPENFILE_SHARE_PASSWORD" && LOGIN=custom
    else
      echo "    Choose the password the share will ask for (user: $DEVICE_USER)."
      smbpasswd "$DEVICE_USER" && LOGIN=custom
    fi
    if [ "$LOGIN" != custom ]; then echo "No password was set, so the share keeps its default." >&2; GUEST=yes; fi
  fi
  if [ "$GUEST" = yes ] && [ "$LOGIN" = custom ] && [ "$LOGIN_ONLY" = yes ]; then
    if [ -f "$SMB_DEV_INCLUDE" ]; then
      echo "The developer shares need a password of your own. Re-run install.sh without --dev-shares first." >&2
      exit 1
    fi
    LOGIN=default
  fi
  if [ "$DEV_SHARES" = yes ] && [ "$LOGIN" != custom ]; then
    echo "    The developer shares reach live code, so they need a password of your own (user: $DEVICE_USER)."
    if smbpasswd "$DEVICE_USER"; then LOGIN=custom; GUEST=no
    else echo "No password was set, so the developer shares were not added." >&2; DEV_SHARES=no; fi
  fi
  [ "$LOGIN" = custom ] || set_share_password "$DEFAULT_SHARE_PASSWORD"
  if grep -q '^OPENFILE_LOGIN=' "$DEFAULTS" 2>/dev/null; then
    sed -i "s/^OPENFILE_LOGIN=.*/OPENFILE_LOGIN=$LOGIN/" "$DEFAULTS"
  else
    echo "OPENFILE_LOGIN=$LOGIN" >> "$DEFAULTS"
  fi
}

write_share() {
  # Where the extra data Macs attach to files is kept. It is only ever metadata,
  # and FAT file ids are reused after a remount, so it starts empty each time.
  rm -rf --one-file-system "$STREAMS"
  install -d -o "$DEVICE_USER" -g "$DEVICE_USER" -m 0750 "$STREAMS"
  render "$INSTALL_DIR/samba/openfile.conf" > "$SMB_INCLUDE"
  grep -qx "include = $SMB_INCLUDE" "$SMB_MAIN" || echo "include = $SMB_INCLUDE" >> "$SMB_MAIN"
  if [ "$DEV_SHARES" = yes ]; then
    render "$INSTALL_DIR/samba/openfile-dev.conf" > "$SMB_DEV_INCLUDE"
    grep -qx "include = $SMB_DEV_INCLUDE" "$SMB_MAIN" || echo "include = $SMB_DEV_INCLUDE" >> "$SMB_MAIN"
  elif [ "$LOGIN_ONLY" = no ]; then
    remove_include "$SMB_DEV_INCLUDE"
  fi
  testparm -s >/dev/null 2>&1 || { echo "Samba rejected the configuration. Run: testparm -s" >&2; exit 1; }
  if grep -q '^OPENFILE_GUEST=' "$DEFAULTS" 2>/dev/null; then
    sed -i "s/^OPENFILE_GUEST=.*/OPENFILE_GUEST=$GUEST/" "$DEFAULTS"
  else
    echo "OPENFILE_GUEST=$GUEST" >> "$DEFAULTS"
  fi
  systemctl enable --quiet smbd
  systemctl restart smbd
}

render() {
  sed -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@MOUNT@|$MOUNT|g" -e "s|@USER@|$DEVICE_USER|g" \
      -e "s|@UID@|$DEVICE_UID|g" -e "s|@HOME@|$DEVICE_HOME|g" -e "s|@CAPS_DIR@|$CAPS_DIR|g" \
      -e "s|@GUEST@|$GUEST|g" -e "s|@VALID_USERS@|$([ "$GUEST" = yes ] || echo "$DEVICE_USER")|g" \
      -e "s|@STREAMS@|$STREAMS|g" "$1"
}

remove_include() {
  [ -f "$SMB_MAIN" ] && sed -i "\|^include = $1\$|d" "$SMB_MAIN"
  rm -f "$1"
}

if [ "$UNINSTALL" = yes ]; then
  for unit in "${UNITS[@]}"; do
    systemctl disable --now "$unit.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/$unit.service"
  done
  systemctl daemon-reload
  remove_include "$SMB_INCLUDE"
  remove_include "$SMB_DEV_INCLUDE"
  systemctl reload smbd 2>/dev/null || true
  rm -f /usr/local/bin/openfile "$DEFAULTS" "$AVAHI_SERVICE" /etc/systemd/system/wsdd2.service.d/openfile.conf
  if [ -f "$NM_ETHERNET" ]; then rm -f "$NM_ETHERNET"; nmcli general reload conf 2>/dev/null || true; fi
  rm -rf --one-file-system "$STREAMS"
  systemctl reload avahi-daemon 2>/dev/null || true
  echo "OpenFile removed. The volume image was left in place."
  exit 0
fi

if [ "$LOGIN_ONLY" = yes ]; then
  configure_login
  write_share
  systemctl restart wsdd2 2>/dev/null || true
  if [ "$GUEST" = yes ]; then echo "Back to the default: guests are let in, and $DEVICE_USER opens it with the password $DEFAULT_SHARE_PASSWORD."
  else echo "The share now asks for the password you chose (user: $DEVICE_USER). Guests are turned away."; fi
  exit 0
fi

echo "==> Packages"
missing=()
command -v mkfs.vfat >/dev/null || missing+=(dosfstools)
command -v smbd >/dev/null      || missing+=(samba)
# Discovery: Avahi is how macOS and Linux find the share, wsdd2 is how Windows does.
command -v avahi-daemon >/dev/null || missing+=(avahi-daemon)
command -v avahi-browse >/dev/null || missing+=(avahi-utils)   # lets `openfile health` see the announcement
command -v wsdd2 >/dev/null        || missing+=(wsdd2)
if [ ${#missing[@]} -gt 0 ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi

echo "==> Settings"
{
  echo "# OpenFile settings. Re-run install.sh to change them."
  echo "OPENHOME_DEVICE_HOME=$DEVICE_HOME"
  echo "LOCAL_CAPABILITIES_DIR=$CAPS_DIR"
  echo "OPENFILE_MOUNT=$MOUNT"
  # Keep what the owner already chose.
  grep -s -E '^OPENFILE_(WEB_TOKEN|GUEST|LOGIN|ALLOW_SELF_EDIT)=' "$DEFAULTS" || true
} > "$DEFAULTS.new"
mv "$DEFAULTS.new" "$DEFAULTS"
chmod 600 "$DEFAULTS"

echo "==> Services"
for unit in openfile-gadget openfile-sync; do
  render "$INSTALL_DIR/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
if [ "$WEB" = yes ]; then
  render "$INSTALL_DIR/systemd/openfile-web.service" > /etc/systemd/system/openfile-web.service
else
  systemctl disable --now openfile-web.service 2>/dev/null || true
  rm -f /etc/systemd/system/openfile-web.service
fi
# Earlier builds offered a Bluetooth receiver and a USB-C gadget. Neither could
# be shown to work on the DevKit, so neither is installed any more.
systemctl disable --now openfile-bluetooth.service 2>/dev/null || true
rm -f /etc/systemd/system/openfile-bluetooth.service
modprobe -r g_mass_storage 2>/dev/null || true
install -m 0755 "$INSTALL_DIR/bin/openfile" /usr/local/bin/openfile
systemctl daemon-reload

# Upgrading: an older build may hold the volume mounted as root, which the
# network share cannot write to, or may have the USB gadget loaded. Release
# both so the volume comes back mounted for the DevKit account.
systemctl stop openfile-web.service openfile-sync.service 2>/dev/null || true
if mountpoint -q "$MOUNT"; then
  sync
  umount "$MOUNT" 2>/dev/null || umount -l "$MOUNT" 2>/dev/null || true
fi
systemctl enable --quiet openfile-gadget.service openfile-sync.service
systemctl restart openfile-gadget.service openfile-sync.service
if [ "$WEB" = yes ]; then
  systemctl enable --quiet openfile-web.service
  systemctl restart openfile-web.service
fi

echo "==> Network share"
configure_login
write_share

if [ "$ETHERNET" = yes ]; then
  echo "==> Wired port"
  # The DevKit firmware ignores eth0 (WiFi only operation, in its own words)
  # with a keyfile rule that a device section cannot outrank. A later file
  # clears that same setting, and the port then gets an address the usual
  # way. Wi-Fi keeps working alongside it.
  printf '# OpenFile: the DevKit firmware ignores the wired port (10-ignore-eth0.conf, WiFi only).\n# A later file clears that setting, so the port is managed and gets an address.\n[keyfile]\nunmanaged-devices=\n' > "$NM_ETHERNET"
  nmcli general reload conf 2>/dev/null || true
  if nmcli -t -f DEVICE,STATE dev status 2>/dev/null | grep -q "^eth0:unmanaged"; then
    systemctl restart NetworkManager
    sleep 5
  fi
  nmcli dev connect eth0 >/dev/null 2>&1 || echo "    No link on the wired port yet. It will connect when a cable is in."
fi

echo "==> Discovery"
WEB_SERVICE=""
[ "$WEB" = yes ] && WEB_SERVICE="  <service>\n    <type>_http._tcp</type>\n    <port>8088</port>\n  </service>\n"
mkdir -p /etc/avahi/services
sed -e "s|@WEB_SERVICE@|$WEB_SERVICE|" "$INSTALL_DIR/avahi/openfile.service" > "$AVAHI_SERVICE"
systemctl enable --quiet avahi-daemon 2>/dev/null || true
systemctl reload avahi-daemon 2>/dev/null || systemctl restart avahi-daemon
if command -v wsdd2 >/dev/null; then
  # wsdd2 reads the Samba name at start and once went quiet during a long run.
  # Keep it restarting, and cycle it after Samba has its final configuration.
  mkdir -p /etc/systemd/system/wsdd2.service.d
  printf '[Service]\nRestart=always\nRestartSec=5\nRuntimeMaxSec=12h\n' > /etc/systemd/system/wsdd2.service.d/openfile.conf
  systemctl daemon-reload
  systemctl enable --quiet wsdd2 2>/dev/null || true
  systemctl restart wsdd2 2>/dev/null || echo "    wsdd2 did not start. Windows will still connect by address."
fi

HOST="$(hostname)"
echo
echo "OpenFile is installed."
echo "  Network share   look for \"$HOST OpenFile\" under Network in Finder, File Explorer or your file manager"
if [ "$GUEST" = yes ]; then
  echo "                  Connect as Guest, or as $DEVICE_USER with the password $DEFAULT_SHARE_PASSWORD."
  echo "                  To choose your own password: openfile password"
else
  echo "                  Login: $DEVICE_USER, with the share password. To remove it: openfile password --remove"
fi
[ "$WEB" = yes ] && echo "  Web page        http://$HOST.local:8088"
echo "  Flash drive     plug one into any USB-A port"
[ "$DEV_SHARES" = yes ] && echo "  Developer       smb://$HOST.local/Abilities-Live and /DevKit-Home (login: $DEVICE_USER)"
[ "$ETHERNET" = yes ] && echo "  Wired port      on. Both the cable and Wi-Fi reach the share."
echo "  Command line    openfile status"
