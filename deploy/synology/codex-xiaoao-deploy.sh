#!/bin/sh
set -eu
umask 077

action=${1:-}
mesh_dir=/volume1/docker/xiaoao-flight-mesh
backup_dir=/volume1/docker/xiaoao-flight-mesh-backups
secret_dir=/volume1/docker/xiaoao-flight-mesh-secrets
runtime_env="$secret_dir/mesh.env"
helper=/usr/local/libexec/codex-xiaoao-cloudflare-tunnel.py
mesh_url=http://127.0.0.1:8789

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
    curl -fsS --max-time 8 "$mesh_url/health"
  else
    wget -qO- -T 8 "$mesh_url/health"
  fi
}

extract_env_value() {
  _path=$1
  _key=$2
  [ -f "$_path" ] || return 0
  awk -v key="$_key" '
    index($0, key "=") == 1 {
      sub("^[^=]*=", "")
      print
      exit
    }
  ' "$_path"
}

generate_token() {
  dd if=/dev/urandom bs=32 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n'
}

ensure_runtime_env() {
  install -d -m 0700 "$secret_dir"
  token=$(extract_env_value "$runtime_env" FLIGHT_MESH_TOKEN)
  if [ "${#token}" -lt 32 ] && [ -f "$mesh_dir/.env" ]; then
    token=$(extract_env_value "$mesh_dir/.env" FLIGHT_MESH_TOKEN)
  fi
  if [ "${#token}" -lt 32 ]; then
    token=$(generate_token)
  fi
  [ "${#token}" -ge 32 ] || { echo "Unable to create a valid Mesh token." >&2; exit 70; }

  serpapi=$(extract_env_value "$runtime_env" SERPAPI_KEY)
  if [ -z "$serpapi" ] && [ -f "$mesh_dir/.env" ]; then
    serpapi=$(extract_env_value "$mesh_dir/.env" SERPAPI_KEY)
  fi

  tmp="$secret_dir/.mesh.env.$$"
  cat > "$tmp" <<EOF
FLIGHT_MESH_TOKEN=$token
FLIGHT_MESH_PROVIDERS=google-playwright,serpapi-google-flights,fast-flights
FLIGHT_MESH_HOST=0.0.0.0
FLIGHT_MESH_PORT=8789
FLIGHT_MESH_BROWSER_TIMEOUT_MS=45000
FLIGHT_MESH_BROWSER_PAGES=3
FLIGHT_MESH_QUERY_CONCURRENCY=4
FLIGHT_MESH_MAX_SEARCHES=12
FLIGHT_MESH_FAST_CONCURRENCY=12
FLIGHT_MESH_FAST_MAX_SEARCHES=60
FLIGHT_MESH_VERIFY_CANDIDATES=6
FLIGHT_MESH_SNAPSHOT_DB=/data/flight-mesh.sqlite3
SERPAPI_KEY=$serpapi
EOF
  chmod 0600 "$tmp"
  mv "$tmp" "$runtime_env"
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
    p=="Dockerfile" || p=="docker-compose.yml" || p=="requirements.txt" || p ~ /^xiaoao_mesh\// { next }
    { exit 21 }
    END { if (count > 240) exit 22 }
  ' || { echo "Mesh archive contains a disallowed path." >&2; exit 65; }
}

mesh_deploy() {
  find_docker
  mkdir -p /volume1/docker "$backup_dir"
  ensure_runtime_env
  work=$(mktemp -d /volume1/docker/.xiaoao-mesh-deploy.XXXXXX)
  archive=$work/release.tgz
  stage=$work/stage
  old=$work/previous
  mkdir -p "$stage"
  trap 'rm -rf "$work"' EXIT HUP INT TERM
  read_archive "$archive"
  validate_mesh_archive "$archive"
  tar -xzf "$archive" -C "$stage"
  [ -f "$stage/Dockerfile" ] && [ -f "$stage/docker-compose.yml" ] && [ -f "$stage/requirements.txt" ] || {
    echo "Mesh archive is missing required files." >&2; exit 65;
  }
  [ ! -e "$stage/.env" ] || { echo "Mesh release must not contain secrets or .env." >&2; exit 65; }
  if find "$stage" -type l | grep -q .; then
    echo "Symbolic links are not allowed in the Mesh archive." >&2
    exit 65
  fi
  if [ -d "$mesh_dir" ]; then
    stamp=$(date +%Y%m%d-%H%M%S)
    tar -czf "$backup_dir/before-$stamp.tgz" -C "$(dirname "$mesh_dir")" "$(basename "$mesh_dir")"
    chmod 0600 "$backup_dir/before-$stamp.tgz" 2>/dev/null || true
    mv "$mesh_dir" "$old"
  fi
  mv "$stage" "$mesh_dir"
  if ! compose -f "$mesh_dir/docker-compose.yml" --env-file "$runtime_env" up -d --build; then
    rm -rf "$mesh_dir"
    if [ -d "$old" ]; then
      mv "$old" "$mesh_dir"
      compose -f "$mesh_dir/docker-compose.yml" --env-file "$runtime_env" up -d >/dev/null 2>&1 || true
    fi
    echo "Mesh deployment failed; the previous release was restored." >&2
    exit 70
  fi
  tries=0
  until health >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
      compose -f "$mesh_dir/docker-compose.yml" --env-file "$runtime_env" logs --tail=80 >&2 || true
      rm -rf "$mesh_dir"
      if [ -d "$old" ]; then
        mv "$old" "$mesh_dir"
        compose -f "$mesh_dir/docker-compose.yml" --env-file "$runtime_env" up -d >/dev/null 2>&1 || true
      fi
      echo "Mesh did not become healthy within 150 seconds; the previous release was restored." >&2
      exit 70
    fi
    sleep 5
  done
  health
  echo
  echo "mesh_deployment=ok"
  echo "mesh_secret_scope=nas-local"
}

mesh_status() {
  find_docker
  if [ ! -f "$mesh_dir/docker-compose.yml" ]; then
    echo "mesh=not-installed"
    [ -f "$runtime_env" ] && echo "mesh_secret=ready" || echo "mesh_secret=not-created"
    exit 0
  fi
  ensure_runtime_env
  compose -f "$mesh_dir/docker-compose.yml" --env-file "$runtime_env" ps
  if health >/dev/null 2>&1; then
    echo "mesh=healthy"
    health | tr -d '\n'
    echo
  else
    echo "mesh=unhealthy"
    exit 1
  fi
  echo "mesh_secret=ready"
}

mesh_benchmark() {
  command -v curl >/dev/null 2>&1 || { echo "curl is required for benchmark." >&2; exit 69; }
  [ -f "$runtime_env" ] || { echo "Mesh runtime secret is not initialized." >&2; exit 66; }
  health >/dev/null
  token=$(extract_env_value "$runtime_env" FLIGHT_MESH_TOKEN)
  [ "${#token}" -ge 32 ] || { echo "Mesh token is invalid." >&2; exit 70; }
  work=$(mktemp -d /volume1/docker/.xiaoao-mesh-benchmark.XXXXXX)
  trap 'rm -rf "$work"' EXIT HUP INT TERM

  cat > "$work/fast.json" <<'EOF'
{"searches":[{"origin":"HKG","destination":"KIX","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"KIX","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"KIX","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"KIX","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"HKG","destination":"ICN","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"ICN","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"ICN","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"ICN","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"HKG","destination":"BKK","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"MFM","destination":"BKK","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"SZX","destination":"BKK","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"CAN","destination":"BKK","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0}]}
EOF
  fast_seconds=$(curl -fsS --max-time 120 -o "$work/fast-response.json" -w '%{time_total}' \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    --data-binary @"$work/fast.json" "$mesh_url/search-fast")
  echo "benchmark_fast_seconds=$fast_seconds"
  fast_coverage=$(tr -d '\n' < "$work/fast-response.json" | sed -n 's/.*"coverage": {"requested": \([0-9][0-9]*\), "completed": \([0-9][0-9]*\), "failed": \([0-9][0-9]*\)}.*/requested=\1 completed=\2 failed=\3/p')
  echo "benchmark_fast_coverage=${fast_coverage:-unparsed}"
  if printf '%s' "$fast_coverage" | grep -q 'completed=0'; then
    echo "Fast discovery returned no completed searches." >&2
    exit 71
  fi

  cat > "$work/verify.json" <<'EOF'
{"searches":[{"origin":"HKG","destination":"KIX","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0},{"origin":"HKG","destination":"ICN","outboundDate":"2026-12-20","returnDate":"2026-12-26","cabin":"economy","adults":2,"children":1,"checkedBags":0}]}
EOF
  verify_seconds=$(curl -fsS --max-time 180 -o "$work/verify-response.json" -w '%{time_total}' \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    --data-binary @"$work/verify.json" "$mesh_url/search-batch")
  echo "benchmark_verify_seconds=$verify_seconds"
  verify_coverage=$(tr -d '\n' < "$work/verify-response.json" | sed -n 's/.*"coverage": {"requested": \([0-9][0-9]*\), "completed": \([0-9][0-9]*\), "failed": \([0-9][0-9]*\)}.*/requested=\1 completed=\2 failed=\3/p')
  echo "benchmark_verify_coverage=${verify_coverage:-unparsed}"
  echo "benchmark=ok"
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
  mesh-benchmark) mesh_benchmark ;;
  cloudflare-deploy) cloudflare_deploy ;;
  tunnel-configure) tunnel_configure ;;
  *) echo "Unsupported Xiaoao deployment action." >&2; exit 64 ;;
esac
