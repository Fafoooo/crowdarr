# Opt-in LXC/macvlan reference deployment

This profile reproduces one operator's environment without making it the project
default. All credentials remain blank and the Crowdarrr IP must be checked for
availability. General installations should start with
`docker-compose.example.yml`.

## Topology

- Docker runs inside an LXC.
- External macvlan network: `media`, subnet `10.10.3.0/24`.
- Host media root: `/home/ubuntu/media`, mounted as `/data` everywhere.
- qBittorrent: `http://10.10.3.22:8080`.
- Radarr: `http://10.10.3.18:7878`, movies under `/data/movies`.
- Sonarr: `http://10.10.3.18:8989`, series under `/data/series`.
- SABnzbd: LAN URL supplied by the operator, downloads under
  `/data/downloads`.
- Optional UmlautAdaptarr: operator-supplied LAN URL.

qBittorrent category/save-path examples:

| Category | Save path |
| --- | --- |
| `radarr` | `/data/downloads/radarr` |
| `sonarr` | `/data/downloads/sonarr` |
| `cross-seed-link` | Operator path below `/data`, commonly `/data/cross-seeds` |
| `autobrr` | Operator path below `/data` |

Stuck cross-seeds in this profile are expected below `/data/cross-seeds/`.

## Start the profile

If the external `media` network already exists, do not recreate it. Otherwise,
adapt the gateway and parent interface to the LXC host before running:

```bash
docker network create -d macvlan \
  --subnet=10.10.3.0/24 \
  --gateway=10.10.3.1 \
  -o parent=eth0 media
```

`10.10.3.1` and `eth0` are examples, not universal values. Confirm the actual
gateway, interface, DHCP pool, and LXC macvlan permissions first.

```bash
cp .env.example .env
mkdir -p config
# Edit CROWDARRR_MACVLAN_IP to an unused 10.10.3.x address.
docker compose -f docker-compose.macvlan.example.yml up -d --build
```

The profile refuses to start until `CROWDARRR_MACVLAN_IP` is set. The service has
no host port publication because its own LAN IP exposes `CROWDARRR_PORT`
directly.

## Configure the UI

Use these values as a starting point:

- CrowdNFO base URL: `https://crowdnfo.net`; paste a profile API key in the UI.
- qBittorrent URL: `http://10.10.3.22:8080`; leave credentials blank only if its
  WebUI auth whitelist genuinely covers the Crowdarrr macvlan address.
- Radarr URL: `http://10.10.3.18:7878`; add its v3 API key.
- Sonarr URL: `http://10.10.3.18:8989`; add its v3 API key.
- SABnzbd: add its actual LAN URL and API key.
- UmlautAdaptarr: optional; enable its changed-title cache and keep its unauthenticated
  title endpoint off the public internet.
- Path mapping: remote `/data` to local `/data`.

Start with dry-run, test each connector, and scan manually before enabling the
backfill schedule. Category mappings in Crowdarrr describe media categories;
qBittorrent remains responsible for category-to-save-path behavior.

## Macvlan caveat

Linux normally prevents direct communication between a macvlan parent host and
its child containers. Other LAN devices can reach Crowdarrr, but the Docker host
may need an explicitly configured macvlan shim. Do not work around this by
silently switching the service to host networking.

## Why there is no VPN sidecar

Crowdarrr does not download media or contact trackers. It makes small,
authenticated HTTPS requests to CrowdNFO and calls connector APIs on the LAN.
Torrent/Usenet traffic remains inside qBittorrent and SABnzbd, where any VPN
policy belongs. A VPN is therefore not required for Crowdarrr; operators can
still route it through one if their own policy demands it.
