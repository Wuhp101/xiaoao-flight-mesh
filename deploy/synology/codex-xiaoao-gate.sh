#!/bin/sh
set -eu

action=${SSH_ORIGINAL_COMMAND:-status}
frontend=/var/services/homes/wuhp101/.ssh/codex-flights-deploy.sh
root_deployer=/usr/local/sbin/codex-xiaoao-deploy

case "$action" in
  status|deploy)
    exec "$frontend"
    ;;
  mesh-status|mesh-deploy|cloudflare-deploy|tunnel-configure)
    exec sudo -n "$root_deployer" "$action"
    ;;
  *)
    echo "Only status, deploy, mesh-status, mesh-deploy, cloudflare-deploy and tunnel-configure are permitted." >&2
    exit 64
    ;;
esac
