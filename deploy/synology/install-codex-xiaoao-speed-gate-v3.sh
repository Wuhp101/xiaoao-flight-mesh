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

frontend=/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh
authorized_keys=/var/services/homes/wuhp101/.ssh/authorized_keys
speed=/usr/local/sbin/codex-xiaoao-speed
gate=/usr/local/sbin/codex-xiaoao-gate
sudoers=/etc/sudoers.d/codex-xiaoao-speed
public_key=AAAAC3NzaC1lZDI1NTE5AAAAIGElc7pguPJWgQCRk3w12zoQ7fb4lW9SMzbvvLcC9NJ7

[ -x "$frontend" ] || { echo "找不到現有小澳機票前端發布器。" >&2; exit 66; }
[ -f "$authorized_keys" ] || { echo "找不到 wuhp101 的 authorized_keys。" >&2; exit 66; }

cat > "$speed" <<'SPEED'
#!/bin/sh
set -eu
umask 077

action=${1:-}
mesh_dir=/volume1/docker/xiaoao-flight-mesh
backup_dir=/volume1/docker/xiaoao-flight-mesh-backups
mesh_url=http://192.168.100.27:8789

find_docker() {
  for candidate in /usr/local/bin/docker /usr/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    if [ -x "$candidate" ]; then DOCKER=$candidate; export DOCKER; return 0; fi
  done
  echo "Container Manager / Docker command was not found." >&2
  exit 69
}

compose() {
  if "$DOCKER" compose version >/dev/null 2>&1; then "$DOCKER" compose "$@"; return; fi
  for candidate in /usr/local/bin/docker-compose /usr/bin/docker-compose /var/packages/ContainerManager/target/usr/bin/docker-compose; do
    if [ -x "$candidate" ]; then "$candidate" "$@"; return; fi
  done
  echo "Docker Compose was not found." >&2
  exit 69
}

health() {
  if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 8 "$mesh_url/health"; else wget -qO- -T 8 "$mesh_url/health"; fi
}

validate_archive() {
  archive=$1
  tar -tzf "$archive" | awk '
    BEGIN { count=0 }
    { p=$0; count++ }
    p ~ /^\// || p ~ /(^|\/)\.\.($|\/)/ { exit 20 }
    p=="Dockerfile" || p=="docker-compose.yml" || p=="requirements.txt" || p ~ /^xiaoao_mesh\// { next }
    { exit 21 }
    END { if (count > 240) exit 22 }
  ' || { echo "Mesh archive contains a disallowed path." >&2; exit 65; }
}

mesh_status() {
  find_docker
  if [ ! -f "$mesh_dir/docker-compose.yml" ]; then echo "mesh=not-installed"; exit 0; fi
  compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" ps
  if health >/dev/null 2>&1; then health; echo; echo "mesh=healthy"; else echo "mesh=unhealthy"; exit 1; fi
}

mesh_deploy() {
  find_docker
  mkdir -p /volume1/docker "$backup_dir"
  work=$(mktemp -d /volume1/docker/.xiaoao-speed-deploy.XXXXXX)
  archive=$work/release.tgz
  stage=$work/stage
  old=$work/previous
  mkdir -p "$stage"
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  dd of="$archive" bs=1048576 count=25 2>/dev/null
  [ -s "$archive" ] || { echo "Deployment archive is empty." >&2; exit 65; }
  validate_archive "$archive"
  tar -xzf "$archive" -C "$stage"
  [ -f "$stage/Dockerfile" ] && [ -f "$stage/docker-compose.yml" ] && [ -f "$stage/requirements.txt" ] || {
    echo "Mesh archive is missing required files." >&2; exit 65;
  }
  if find "$stage" -type l | grep -q .; then echo "Symbolic links are not allowed in the Mesh archive." >&2; exit 65; fi
  [ -f "$mesh_dir/.env" ] || { echo "Existing NAS Mesh .env is required; refusing to replace secrets." >&2; exit 66; }
  cp "$mesh_dir/.env" "$stage/.env"
  chmod 600 "$stage/.env"
  echo "mesh_env=preserved"
  stamp=$(date +%Y%m%d-%H%M%S)
  tar -czf "$backup_dir/before-$stamp.tgz" -C "$(dirname "$mesh_dir")" "$(basename "$mesh_dir")"
  mv "$mesh_dir" "$old"
  mv "$stage" "$mesh_dir"
  if ! compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" up -d --build; then
    rm -rf "$mesh_dir"
    mv "$old" "$mesh_dir"
    compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" up -d >/dev/null 2>&1 || true
    echo "Mesh deployment failed; the previous release was restored." >&2
    exit 70
  fi
  tries=0
  until health >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
      compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" logs --tail=80 >&2 || true
      echo "Mesh did not become healthy within 150 seconds." >&2
      exit 70
    fi
    sleep 5
  done
  health
  echo
  echo "mesh_deployment=ok"
}

mesh_token() {
  token=$(sed -n 's/^FLIGHT_MESH_TOKEN=//p' "$mesh_dir/.env" | head -n 1)
  [ "${#token}" -ge 32 ] || { echo "Mesh token is missing or too short." >&2; exit 66; }
  printf '%s' "$token"
}

run_fast() {
  label=$1
  payload=$2
  outfile=$3
  token=$4
  metrics=$(curl -sS --max-time 75 -o "$outfile" \
    -w "benchmark_${label}_http=%{http_code}\nbenchmark_${label}_seconds=%{time_total}\n" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data "$payload" "$mesh_url/search-fast")
  printf '%s\n' "$metrics"
  printf '%s\n' "$metrics" | grep -q "benchmark_${label}_http=200" || exit 70
  grep -q 'fast-discovery' "$outfile" || { echo "Speed Engine V2 response not detected." >&2; exit 70; }
  requested=$(grep -o '"requested":[[:space:]]*[0-9][0-9]*' "$outfile" | head -n 1 | grep -o '[0-9][0-9]*' || true)
  completed=$(grep -o '"completed":[[:space:]]*[0-9][0-9]*' "$outfile" | head -n 1 | grep -o '[0-9][0-9]*' || true)
  [ -n "$requested" ] && echo "benchmark_${label}_requested=$requested"
  [ -n "$completed" ] && echo "benchmark_${label}_completed=$completed"
}

mesh_benchmark() {
  command -v curl >/dev/null 2>&1 || { echo "curl is required for benchmark." >&2; exit 69; }
  health_json=$(health)
  printf '%s\n' "$health_json" | grep -q '"speedEngineVersion"[[:space:]]*:[[:space:]]*"v2"' || {
    echo "Speed Engine V2 is not active." >&2; exit 70;
  }
  token=$(mesh_token)
  work=$(mktemp -d /volume1/docker/.xiaoao-benchmark.XXXXXX)
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  one='{"searches":[{"origin":"HKG","destination":"KIX","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0}]}'
  batch='{"searches":[{"origin":"HKG","destination":"KIX","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"HKG","destination":"ICN","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"HKG","destination":"BKK","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"KIX","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"ICN","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"BKK","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"KIX","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"ICN","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"BKK","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"KIX","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"ICN","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"BKK","outboundDate":"2026-12-19","returnDate":"2026-12-25","cabin":"economy","adults":2,"children":1,"checkedBags":0}]}'
  run_fast first "$one" "$work/first.json" "$token"
  run_fast batch12 "$batch" "$work/batch12.json" "$token"
  echo "mesh_benchmark=ok"
}

case "$action" in
  mesh-status) mesh_status ;;
  mesh-deploy) mesh_deploy ;;
  mesh-benchmark) mesh_benchmark ;;
  *) echo "Unsupported Xiaoao speed action." >&2; exit 64 ;;
esac
SPEED
chmod 0755 "$speed"
chown root:root "$speed"

cat > "$gate" <<'GATE'
#!/bin/sh
set -eu
action=${SSH_ORIGINAL_COMMAND:-status}
frontend=/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh
speed=/usr/local/sbin/codex-xiaoao-speed
case "$action" in
  status|deploy) exec "$frontend" ;;
  mesh-status|mesh-deploy|mesh-benchmark) exec sudo -n "$speed" "$action" ;;
  *) echo "Only status, deploy, mesh-status, mesh-deploy and mesh-benchmark are permitted." >&2; exit 64 ;;
esac
GATE
chmod 0755 "$gate"
chown root:root "$gate"

mkdir -p /etc/sudoers.d
cat > "$sudoers" <<EOF
wuhp101 ALL=(root) NOPASSWD: $speed mesh-status
wuhp101 ALL=(root) NOPASSWD: $speed mesh-deploy
wuhp101 ALL=(root) NOPASSWD: $speed mesh-benchmark
EOF
chmod 0440 "$sudoers"
if command -v visudo >/dev/null 2>&1; then visudo -cf "$sudoers" >/dev/null; elif [ -x /usr/sbin/visudo ]; then /usr/sbin/visudo -cf "$sudoers" >/dev/null; fi

cp -p "$authorized_keys" "$authorized_keys.before-xiaoao-speed-$(date +%Y%m%d-%H%M%S)"
awk -v key="$public_key" -v gate="$gate" '
  index($0, key) {
    print "command=\"" gate "\",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 " key " xiaoao-speed-gate-v3"
    found=1
    next
  }
  { print }
  END { if (!found) exit 42 }
' "$authorized_keys" > "$authorized_keys.new" || {
  rm -f "$authorized_keys.new"
  echo "找不到既有小澳部署金鑰；沒有改動 authorized_keys。" >&2
  exit 66
}
chown wuhp101:users "$authorized_keys.new" 2>/dev/null || chown wuhp101 "$authorized_keys.new"
chmod 0600 "$authorized_keys.new"
mv "$authorized_keys.new" "$authorized_keys"

if command -v su >/dev/null 2>&1; then
  su -s /bin/sh -c 'SSH_ORIGINAL_COMMAND=mesh-status /usr/local/sbin/codex-xiaoao-gate' wuhp101 || {
    echo "gate_self_test=failed" >&2
    exit 70
  }
fi

echo "xiaoao_speed_gate=installed"
echo "allowed=status,deploy,mesh-status,mesh-deploy,mesh-benchmark"
