#!/bin/sh
set -eu
umask 077

log=/volume1/web/flights/xiaoao-install-result.txt
if [ "$(id -u)" -eq 0 ] && [ -d /volume1/web/flights ]; then
  : > "$log"
  chmod 0644 "$log"
  exec >> "$log" 2>&1
fi

[ "$(id -u)" -eq 0 ] || { echo "請把工作排程器的使用者設為 root。" >&2; exit 77; }
echo "installer_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The active GitHub Actions key changed after the original August bootstrap.
# Accept the exact public key as argv[1], with the current production key as the
# safe default. Public keys are not secrets; private key material never enters NAS logs.
target_public_key=${1:-'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPOTAH74HilOUbT2AAoMhVV/vydaDCIsmc4Ko1f04e6d github-actions-deploy@wuhp101-web'}
case "$target_public_key" in
  ssh-ed25519\ *) ;;
  *) echo "invalid_target_public_key" >&2; exit 64 ;;
esac
key_material=$(printf '%s\n' "$target_public_key" | awk 'NF >= 2 { print $2; exit }')
case "$key_material" in
  ''|*[!A-Za-z0-9+/=]*) echo "invalid_target_public_key_material" >&2; exit 64 ;;
esac
echo "target_key_material=$key_material"

revision=42f030b508cae3190c7bfbc73832ee8cd0fc4ccc
base="https://raw.githubusercontent.com/Wuhp101/xiaoao-flight-mesh/$revision/deploy/synology"
work=$(mktemp -d /tmp/xiaoao-speed-gate-install.XXXXXX)
trap 'rm -rf "$work"' EXIT HUP INT TERM

fetch() {
  name=$1
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 "$base/$name" -o "$work/$name"
  else
    wget -qO "$work/$name" "$base/$name"
  fi
}

git_blob_sha1() {
  file=$1
  size=$(wc -c < "$file" | tr -d ' ')
  { printf 'blob %s\000' "$size"; cat "$file"; } | sha1sum | cut -d' ' -f1
}

fetch codex-xiaoao-gate.sh
fetch codex-xiaoao-deploy.sh
fetch cloudflare_tunnel.py

[ "$(git_blob_sha1 "$work/codex-xiaoao-gate.sh")" = "920b145e933a6594f3683ae7835347e263b24a50" ] || {
  echo "gate_blob_mismatch" >&2; exit 65;
}
[ "$(git_blob_sha1 "$work/codex-xiaoao-deploy.sh")" = "0e9c566f3850a3f63df96a0c0dd0c26e77458551" ] || {
  echo "deployer_blob_mismatch" >&2; exit 65;
}
[ "$(git_blob_sha1 "$work/cloudflare_tunnel.py")" = "0122f102333d5812615892fc95ee29716e5a0667" ] || {
  echo "tunnel_helper_blob_mismatch" >&2; exit 65;
}

sh -n "$work/codex-xiaoao-gate.sh"
sh -n "$work/codex-xiaoao-deploy.sh"

frontend=/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh
authorized_keys=/var/services/homes/wuhp101/.ssh/authorized_keys
[ -x "$frontend" ] || { echo "找不到現有小澳機票前端發布器。" >&2; exit 66; }
[ -f "$authorized_keys" ] || { echo "找不到 wuhp101 的 authorized_keys。" >&2; exit 66; }

install -o root -g root -m 0755 "$work/codex-xiaoao-gate.sh" /usr/local/sbin/codex-xiaoao-gate
install -o root -g root -m 0755 "$work/codex-xiaoao-deploy.sh" /usr/local/sbin/codex-xiaoao-deploy
mkdir -p /usr/local/libexec
install -o root -g root -m 0644 "$work/cloudflare_tunnel.py" /usr/local/libexec/codex-xiaoao-cloudflare-tunnel.py

sudoers_tmp=$work/codex-xiaoao.sudoers
{
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy mesh-status"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy mesh-deploy"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy mesh-benchmark"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy cloudflare-deploy"
  echo "wuhp101 ALL=(root) NOPASSWD: /usr/local/sbin/codex-xiaoao-deploy tunnel-configure"
} > "$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
mkdir -p /etc/sudoers.d
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$sudoers_tmp" >/dev/null
elif [ -x /usr/sbin/visudo ]; then
  /usr/sbin/visudo -cf "$sudoers_tmp" >/dev/null
else
  echo "visudo=not-found; installing fixed allowlist without parser validation"
fi
install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/codex-xiaoao

cp -p "$authorized_keys" "$authorized_keys.before-xiaoao-speed-v3-$(date +%Y%m%d-%H%M%S)"
awk -v key="$key_material" '
  index($0, key) {
    print "command=\"/usr/local/sbin/codex-xiaoao-gate\",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 " key " xiaoao-speed-gate-v3"
    found=1
    next
  }
  { print }
  END { if (!found) exit 42 }
' "$authorized_keys" > "$work/authorized_keys" || {
  echo "找不到目前 GitHub Actions 使用的小澳部署金鑰；沒有改動 authorized_keys。" >&2
  exit 66
}
if ! chown wuhp101:users "$work/authorized_keys" 2>/dev/null; then
  chown wuhp101 "$work/authorized_keys"
fi
chmod 0600 "$work/authorized_keys"
mv "$work/authorized_keys" "$authorized_keys"

if command -v su >/dev/null 2>&1; then
  su -s /bin/sh -c 'SSH_ORIGINAL_COMMAND=mesh-status /usr/local/sbin/codex-xiaoao-gate' wuhp101 || {
    echo "gate_self_test=failed" >&2
    exit 70
  }
fi

echo "xiaoao_speed_gate_v3=installed"
echo "mesh_token_storage=/volume1/docker/xiaoao-flight-mesh-secrets/mesh.env"
echo "allowed=status,deploy,mesh-status,mesh-deploy,mesh-benchmark,cloudflare-deploy,tunnel-configure"
