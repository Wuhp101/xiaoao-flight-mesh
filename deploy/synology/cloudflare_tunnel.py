from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_env(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.replace("_", "").isalnum():
                raise ValueError("invalid tunnel configuration line")
            result[key] = value
    return result


class Cloudflare:
    def __init__(self, token: str):
        if not token:
            raise ValueError("CLOUDFLARE_API_TOKEN is required")
        self.token = token
        self.base = "https://api.cloudflare.com/client/v4"

    def request(self, method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Cloudflare API {error.code}: {detail}") from error
        if not body.get("success"):
            raise RuntimeError(f"Cloudflare API rejected the request: {body.get('errors')}")
        return body.get("result")


def find_tunnel(api: Cloudflare, account: str, existing_hostname: str):
    tunnels = api.request("GET", f"/accounts/{account}/cfd_tunnel?is_deleted=false&per_page=100") or []
    for tunnel in tunnels:
        tunnel_id = tunnel.get("id")
        if not tunnel_id:
            continue
        configuration = api.request("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations") or {}
        config = configuration.get("config") or {}
        if any(item.get("hostname") == existing_hostname for item in config.get("ingress") or []):
            return tunnel_id, config
    raise RuntimeError(f"No tunnel contains the existing hostname {existing_hostname}")


def configure_ingress(api: Cloudflare, account: str, tunnel_id: str, config: dict, hostname: str, service: str):
    ingress = [item for item in (config.get("ingress") or []) if item.get("hostname") != hostname]
    catch_all = None
    if ingress and not ingress[-1].get("hostname"):
        catch_all = ingress.pop()
    ingress.append({"hostname": hostname, "service": service})
    ingress.append(catch_all or {"service": "http_status:404"})
    updated = dict(config)
    updated["ingress"] = ingress
    api.request("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", {"config": updated})


def configure_dns(api: Cloudflare, account: str, zone_name: str, tunnel_id: str, hostname: str):
    query = urllib.parse.urlencode({"name": zone_name, "account.id": account, "per_page": 50})
    zones = api.request("GET", f"/zones?{query}") or []
    if len(zones) != 1:
        raise RuntimeError(f"Expected one Cloudflare zone for {zone_name}, found {len(zones)}")
    zone_id = zones[0]["id"]
    query = urllib.parse.urlencode({"type": "CNAME", "name": hostname, "per_page": 100})
    records = api.request("GET", f"/zones/{zone_id}/dns_records?{query}") or []
    payload = {"type": "CNAME", "name": hostname, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True, "ttl": 1}
    if records:
        api.request("PUT", f"/zones/{zone_id}/dns_records/{records[0]['id']}", payload)
    else:
        api.request("POST", f"/zones/{zone_id}/dns_records", payload)


def main() -> None:
    values = load_env(sys.argv[1])
    account = values.get("CLOUDFLARE_ACCOUNT_ID", "")
    hostname = values.get("FLIGHT_MESH_HOSTNAME", "flight-mesh.wuhp101.com")
    service = values.get("FLIGHT_MESH_SERVICE", "http://192.168.100.27:8789")
    existing = values.get("EXISTING_TUNNEL_HOSTNAME", "nas.wuhp101.com")
    zone = values.get("CLOUDFLARE_ZONE", "wuhp101.com")
    if not account:
        raise ValueError("CLOUDFLARE_ACCOUNT_ID is required")
    api = Cloudflare(values.get("CLOUDFLARE_API_TOKEN", ""))
    tunnel_id, config = find_tunnel(api, account, existing)
    configure_ingress(api, account, tunnel_id, config, hostname, service)
    configure_dns(api, account, zone, tunnel_id, hostname)
    print(json.dumps({"ok": True, "hostname": hostname, "service": service, "tunnel": tunnel_id}))


if __name__ == "__main__":
    main()
