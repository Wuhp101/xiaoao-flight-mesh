#!/bin/sh
set -eu
umask 077

action=${1:-}
mesh_dir=/volume1/docker/xiaoao-flight-mesh
backup_dir=/volume1/docker/xiaoao-flight-mesh-backups
helper=/usr/local/libexec/codex-xiaoao-cloudflare-tunnel.py

find_docker() {
  for candidate in /usr/local/bin/docker /usr/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    if [ -x "$candidate" ]; then
      DOCKER=$candidate
      export DOCKER
      return 0
    fi
  done
  echo "Container Manager / Docker command was not found." >&2
  exit 69
}

compose() {
  if "$DOCKER" compose version >/dev/null 2>&1; then
    "$DOCKER" compose "$@"
    return
  fi
  for candidate in /usr/local/bin/docker-compose /usr/bin/docker-compose /var/packages/ContainerManager/target/usr/bin/docker-compose; do
    if [ -x "$candidate" ]; then
      "$candidate" "$@"
      return
    fi
  done
  echo "Docker Compose was not found." >&2
  exit 69
}

health() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 8 http://192.168.100.27:8789/health
  else
    wget -qO- -T 8 http://192.168.100.27:8789/health
  fi
}

read_archive() {
  archive=$1
  dd of="$archive" bs=1048576 count=25 2>/dev/null
  [ -s "$archive" ] || { echo "Deployment archive is empty." >&2; exit 65; }
  tar -tzf "$archive" >/dev/null
}

validate_mesh_archive() {
  archive=$1
  tar -tzf "$archive" | awk '
    BEGIN { count=0 }
    { p=$0; count++ }
    p ~ /^\// || p ~ /(^|\/)\.\.($|\/)/ { exit 20 }
    p=="Dockerfile" || p=="docker-compose.yml" || p=="requirements.txt" || p==".env" || p ~ /^xiaoao_mesh\// { next }
    { exit 21 }
    END { if (count > 240) exit 22 }
  ' || { echo "Mesh archive contains a disallowed path." >&2; exit 65; }
}

mesh_deploy() {
  find_docker
  mkdir -p /volume1/docker "$backup_dir"
  work=$(mktemp -d /volume1/docker/.xiaoao-mesh-deploy.XXXXXX)
  archive=$work/release.tgz
  stage=$work/stage
  old=$work/previous
  mkdir -p "$stage"
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  read_archive "$archive"
  validate_mesh_archive "$archive"
  tar -xzf "$archive" -C "$stage"
  [ -f "$stage/Dockerfile" ] && [ -f "$stage/docker-compose.yml" ] && [ -f "$stage/requirements.txt" ] && [ -f "$stage/.env" ] || {
    echo "Mesh archive is missing required files." >&2; exit 65;
  }
  if find "$stage" -type l | grep -q .; then
    echo "Symbolic links are not allowed in the Mesh archive." >&2
    exit 65
  fi
  chmod 600 "$stage/.env"
  if [ -d "$mesh_dir" ]; then
    stamp=$(date +%Y%m%d-%H%M%S)
    tar -czf "$backup_dir/before-$stamp.tgz" -C "$(dirname "$mesh_dir")" "$(basename "$mesh_dir")"
    mv "$mesh_dir" "$old"
  fi
  mv "$stage" "$mesh_dir"
  if ! compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" up -d --build; then
    rm -rf "$mesh_dir"
    if [ -d "$old" ]; then
      mv "$old" "$mesh_dir"
      compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" up -d >/dev/null 2>&1 || true
    fi
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

mesh_status() {
  find_docker
  if [ ! -f "$mesh_dir/docker-compose.yml" ]; then
    echo "mesh=not-installed"
    exit 0
  fi
  compose -f "$mesh_dir/docker-compose.yml" --env-file "$mesh_dir/.env" ps
  if health >/dev/null 2>&1; then
    echo "mesh=healthy"
  else
    echo "mesh=unhealthy"
    exit 1
  fi
}

validate_cloudflare_archive() {
  archive=$1
  tar -tzf "$archive" | awk '
    BEGIN { count=0 }
    { p=$0; count++ }
    p ~ /^\// || p ~ /(^|\/)\.\.($|\/)/ { exit 20 }
    p=="wrangler.jsonc" || p==".cf.env" || p==".mesh-token" || p=="src/worker.js" { next }
    { exit 21 }
    END { if (count > 8) exit 22 }
  ' || { echo "Cloudflare archive contains a disallowed path." >&2; exit 65; }
}

cloudflare_deploy() {
  find_docker
  work=$(mktemp -d /volume1/docker/.xiaoao-cloudflare-deploy.XXXXXX)
  archive=$work/release.tgz
  stage=$work/stage
  mkdir -p "$stage"
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  read_archive "$archive"
  validate_cloudflare_archive "$archive"
  tar -xzf "$archive" -C "$stage"
  [ -f "$stage/wrangler.jsonc" ] && [ -f "$stage/src/worker.js" ] && [ -f "$stage/.cf.env" ] && [ -f "$stage/.mesh-token" ] || {
    echo "Cloudflare archive is missing required files." >&2; exit 65;
  }
  chmod 600 "$stage/.cf.env" "$stage/.mesh-token"
  "$DOCKER" run --rm -i --env-file "$stage/.cf.env" -v "$stage:/work" -w /work node:22-bookworm-slim \
    sh -lc 'npm_config_cache=/tmp/npm-cache npx --yes wrangler@4 secret put FLIGHT_MESH_TOKEN' < "$stage/.mesh-token"
  "$DOCKER" run --rm --env-file "$stage/.cf.env" -v "$stage:/work" -w /work node:22-bookworm-slim \
    sh -lc 'npm_config_cache=/tmp/npm-cache npx --yes wrangler@4 deploy --keep-vars'
  echo "cloudflare_deployment=ok"
}

tunnel_configure() {
  find_docker
  work=$(mktemp -d /volume1/docker/.xiaoao-tunnel-configure.XXXXXX)
  input=$work/tunnel.env
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  dd of="$input" bs=8192 count=1 2>/dev/null
  chmod 600 "$input"
  [ -s "$input" ] || { echo "Tunnel configuration is empty." >&2; exit 65; }
  "$DOCKER" run --rm -v "$helper:/app/configure.py:ro" -v "$input:/input/tunnel.env:ro" python:3.12-alpine \
    python /app/configure.py /input/tunnel.env
}

case "$action" in
  mesh-status) mesh_status ;;
  mesh-deploy) mesh_deploy ;;
  cloudflare-deploy) cloudflare_deploy ;;
  tunnel-configure) tunnel_configure ;;
  *) echo "Unsupported Xiaoao deployment action." >&2; exit 64 ;;
esac
