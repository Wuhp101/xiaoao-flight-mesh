#!/bin/sh
set -eu
umask 077

[ "$(id -u)" -eq 0 ] || { echo "請把工作排程器的使用者設為 root。" >&2; exit 77; }

revision=f616f7d656af57502adf2cbecc63f288a5c5f0bc
base="https://raw.githubusercontent.com/Wuhp101/xiaoao-flight-mesh/$revision/deploy/synology"
work=$(mktemp -d /tmp/xiaoao-gate-install.XXXXXX)
trap 'rm -rf "$work"' EXIT HUP INT TERM

fetch() {
  name=$1
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 "$base/$name" -o "$work/$name"
  else
    wget -qO "$work/$name" "$base/$name"
  fi
}

fetch codex-xiaoao-gate.sh
fetch codex-xiaoao-deploy.sh
fetch cloudflare_tunnel.py

cd "$work"
echo "d05510019f05b90370f7a96b93deb5e22357085b995fe2c23ce796dda228793c  codex-xiaoao-gate.sh" | sha256sum -c -
echo "3382db6e9b58edbcc179be9b60ae53c1dc65a5621db921a7a738cb89c72bc2ff  codex-xiaoao-deploy.sh" | sha256sum -c -
echo "e93ab023e7d4e545f3b4db22df249ef27ca7618047602f32aef7a3eb9f9e71cc  cloudflare_tunnel.py" | sha256sum -c -
sh -n codex-xiaoao-gate.sh
sh -n codex-xiaoao-deploy.sh

frontend=/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh
authorized_keys=/var/services/homes/wuhp101/.ssh/authorized_keys
[ -x "$frontend" ] || { echo "找不到現有小澳機票前端發布器。" >&2; exit 66; }
[ -f "$authorized_keys" ] || { echo "找不到 wuhp101 的 authorized_keys。" >&2; exit 66; }

install -o root -g root -m 0755 codex-xiaoao-gate.sh /usr/local/sbin/codex-xiaoao-gate
install -o root -g root -m 0755 codex-xiaoao-deploy.sh /usr/local/sbin/codex-xiaoao-deploy
mkdir -p /usr/local/libexec
install -o root -g root -m 0644 cloudflare_tunnel.py /usr/local/libexec/codex-xiaoao-cloudflare-tunnel.py

sudoers_tmp=$work/codex-xiaoao.sudoers
{
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy mesh-status"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy mesh-deploy"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy cloudflare-deploy"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy tunnel-configure"
} > "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$sudoers_tmp" >/dev/null
elif [ -x /usr/sbin/visudo ]; then
  /usr/sbin/visudo -cf "$sudoers_tmp" >/dev/null
else
  echo "找不到 visudo，為安全起見沒有修改 sudo 權限。" >&2
  exit 69
fi
install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/codex-xiaoao

cp -p "$authorized_keys" "$authorized_keys.before-xiaoao-$(date +%Y%m%d-%H%M%S)"
if grep -q 'command="/usr/local/sbin/codex-xiaoao-gate"' "$authorized_keys"; then
  :
elif grep -q 'command="/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh"' "$authorized_keys"; then
  sed 's#command="/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh"#command="/usr/local/sbin/codex-xiaoao-gate"#' "$authorized_keys" > "$work/authorized_keys"
  chown wuhp101:users "$work/authorized_keys"
  chmod 0600 "$work/authorized_keys"
  mv "$work/authorized_keys" "$authorized_keys"
else
  echo "找不到受限小澳機票金鑰；沒有改動 authorized_keys。" >&2
  exit 66
fi

echo "xiaoao_deployer=installed"
echo "allowed=status,deploy,mesh-status,mesh-deploy,cloudflare-deploy,tunnel-configure"
