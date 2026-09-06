# LAN-Manager

**English** · [Italiano](README.it.md)

Self-hosted dashboard to **manage, monitor and map your home or small-office LAN**.
Runs in Docker, reachable **from your private network only**, and is meant to be
configured from its own web UI rather than by editing files.

> **Version 1.0.0** - the first public release. See [CHANGELOG.md](CHANGELOG.md).
>
> Not in this release, and honest about it: the first-run wizard described below
> is not built yet, so the initial configuration is done by copying and editing a
> template, and the interface is currently in Italian only. Both land in `1.1.0`.

## What it does

- **Devices** — everything on your LAN, online or offline, with a detail card per
  device (IP, MAC, vendor, OS guess, open ports, where it was discovered, last
  seen) that refreshes live while the device is up. You can add, edit, hide and
  remove devices freely.
- **Discovery** — DHCP leases and ARP from the router, plus a ping sweep, nmap,
  reverse DNS, SSH facts and SNMP. Each source is a pluggable provider and failures
  in one never take down the others.
- **Interactive map** — the network drawn as a graph you can pan, zoom and rearrange.
- **Traffic and WAN** — interface counters and throughput from an OpenWrt router,
  plus the state of the internet uplink (wired, 4G, tethering).
- **Services** — for every service you choose **how** to monitor it (Docker
  container, ping, TCP port, HTTP status, systemd unit) and **where**, including on
  other hosts of the LAN. Docker containers show up on their own.
- **Host resources** — CPU, RAM, disks and temperatures of your machines, over SSH.
- **WireGuard** — peer status, read from wherever WireGuard actually runs, including
  on the router itself.
- **Logs** — router syslog, the application's own log, the audit trail and
  `journalctl` from LAN hosts, in one filterable view.
- **Network tools** — ping, traceroute, dig, port scan, TCP port check, ARP ping,
  whois, HTTP inspection and a line speed measurement, with streaming output.
- **SSH terminal** in the browser, restricted to an allowlist of hosts, with an
  audit log of every command.
- **Settings** — the whole configuration is editable from the UI, with validation,
  automatic backups and secrets that are never sent back to the browser.

## Design principles

1. **Real data only.** No demo mode, no mock data. When a source cannot be reached
   the app degrades with an explicit warning and an "unavailable" state — never with
   invented numbers.
2. **Nothing hardcoded.** Names, addresses, subnets, hosts, services and credentials
   live in configuration, never in the code. If your network changes, you change the
   configuration — ideally from the UI — and nothing else.
3. **Private network only.** This is not built to face the internet, and the
   documentation will keep saying so.
4. **Bilingual.** English and Italian, switchable at runtime, across the interface,
   the API error messages, the example configuration and the documentation.

## Requirements

- Docker and the Docker Compose plugin
- A private network you are allowed to scan
- Optional, for the features that need them: an OpenWrt router reachable over SSH,
  SSH access to the hosts you want resource metrics from, SNMP-capable devices

## Quick start

```sh
git clone https://github.com/Yul-1/LAN-Manager.git
cd LAN-Manager

# Your configuration. Edit at least the `subnets` block: the file ships with
# RFC 5737 documentation ranges, which match no real network on purpose.
cp config/config.example.yaml config/config.yaml

docker compose up --build
```

The dashboard is then on port 80 of the host. On first visit it asks you to
choose the admin password, which is stored as a bcrypt hash in
`config/secrets.env` and never in `config.yaml`.

If the host already runs its own nginx on port 80, use the alternative profile
`deploy/docker-compose.host-nginx.yml`, which starts the backend only and lets
your nginx serve the frontend — see the comments in that file.

## Configuration

Everything is editable from the **Settings** page. If you prefer files, the
templates are in `config/`:

| File | What it holds |
|---|---|
| `config/config.example.yaml` | Main configuration, fully commented |
| `config/devices.example.yaml` | Device catalogue: names and preferences per address |
| `config/services.example.yaml` | Services to monitor and how to monitor each one |

Copy a template to the same name without `.example` and edit it. Every value can
also be overridden by an environment variable prefixed with `LAN_`; nested sections
use a double underscore, for example `LAN_ROUTER__HOST`.

Secrets (router password, admin password hash) belong in `config/secrets.env`, never
in `config.yaml` and never in the repository. The API only ever reports whether a
secret is set, never its value.

## Security

- **Expose it on your LAN only.** Do not put it on the internet, and do not expose
  the Docker TCP socket.
- The **admin login stays on** by default. The app deliberately does not offer
  "trust anything on the local network" as a shortcut: a LAN includes segmented
  subnets and VPN clients.
- If the configuration is permissive, the backend logs a warning at startup and the
  dashboard shows a banner, so an insecure setup cannot go unnoticed.
- The SSH terminal and the network tools are sensitive surface: both require
  authentication and both are audited.

Found a security problem? Open an issue without a working exploit, or contact the
maintainer privately.

## License

[MIT](LICENSE)
