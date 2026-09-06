# Changelog

All notable changes to LAN-Manager are documented here.
This file describes **what changes for people who run the app** — not the
development history.

Tutte le modifiche rilevanti di LAN-Manager sono documentate qui.
Questo file descrive **cosa cambia per chi usa l'app**, non la cronaca dello sviluppo.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [Unreleased]

Planned for `1.1.0`: a guided first-run setup, and an English interface
alongside the Italian one.

Previsto per la `1.1.0`: configurazione guidata al primo avvio, e interfaccia in
inglese accanto a quella italiana.

## [1.0.0] - 2026-09-06

First public release.
Primo rilascio pubblico.

### Added
- LAN device discovery from several independent sources — DHCP leases and ARP
  from an OpenWrt router, ping sweep, nmap, reverse DNS, SSH facts, SNMP — each
  isolated, so one failing source never takes the others down.
- A detail card per device that refreshes live while the device is up, and an
  interactive network map.
- Service monitoring where you choose **how** to check each service (Docker
  container, ping, TCP port, HTTP status, systemd unit) and **on which host**,
  including hosts other than the one running the app.
- Docker monitoring across several hosts over SSH, with no Docker port exposed.
- Host resources (CPU, RAM, disks, temperatures) over SSH, Linux and Windows.
- Traffic, WAN state and WireGuard peer status, read from wherever WireGuard
  actually runs, including on the router.
- One filterable Logs page over four sources: router syslog, the application's
  own log, the audit trail, and `journalctl` from LAN hosts.
- Network tools with streaming output (ping, traceroute, dig, port scan, TCP
  port check, ARP ping, whois, HTTP inspection, line speed) and an SSH terminal
  in the browser, both restricted to known hosts and both audited.
- The whole configuration editable from the Settings page, with validation,
  automatic backups, and secrets that are never sent back to the browser.
- `docker compose up --build` builds and starts the stack from a fresh clone.

### Security
- The admin login is on by default. The app deliberately does not offer
  "trust anything on the local network" as a shortcut: a LAN includes segmented
  subnets, guest wifi and VPN clients.
- A permissive configuration is reported in the log at startup and shown as a
  banner in the dashboard, so an insecure setup cannot go unnoticed.
- Secrets live in `config/secrets.env`, never in `config.yaml`; the API only
  ever reports whether a secret is set, never its value.

### Notes
- The interface is currently in Italian. Configuration templates and
  documentation are available in English and Italian.
- Example configuration ships with RFC 5737 documentation addresses, which match
  no real network on purpose: edit `subnets` before the first run.

## [0.1.0] - 2026-09-06

### Added
- Repository initialized under the MIT License.
- Repository inizializzato con licenza MIT.

[Unreleased]: https://github.com/Yul-1/LAN-Manager/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Yul-1/LAN-Manager/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/Yul-1/LAN-Manager/releases/tag/v0.1.0
