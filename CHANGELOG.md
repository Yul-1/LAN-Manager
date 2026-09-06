# Changelog

All notable changes to LAN-Manager are documented here.
This file describes **what changes for people who run the app** — not the
development history.

Tutte le modifiche rilevanti di LAN-Manager sono documentate qui.
Questo file descrive **cosa cambia per chi usa l'app**, non la cronaca dello sviluppo.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [Unreleased]

### Added
- **Prebuilt images on GHCR** (`linux/amd64`), published on every release:
  `docker compose pull && docker compose up -d` instead of building. Building
  locally with `docker compose up --build` still works and is the only route on
  ARM hosts.
- **Continuous integration**: the backend suite, the frontend suite and a check
  that both example configurations still validate and stay in step with their
  translated copy, run on every push and pull request.
- The release workflow refuses to publish when the tag does not match `VERSION`,
  and starts the image it just built to check that `/health` answers with the
  expected version before the release is marked green.

### Changed
- Images are now named after where they are published:
  `ghcr.io/yul-1/lan-manager-backend` and `-nginx`. One name covers pulling,
  building locally, and `docker save`/`docker load` for offline installs.

### Aggiunto
- **Immagini gia' pronte su GHCR** (`linux/amd64`), pubblicate ad ogni rilascio:
  `docker compose pull && docker compose up -d` invece di compilare. La build
  locale con `docker compose up --build` continua a funzionare, ed e' l'unica
  strada sugli host ARM.
- **Integrazione continua**: suite backend, suite frontend e un controllo che le
  configurazioni di esempio restino valide e allineate alla copia tradotta, ad
  ogni push e pull request.
- Il workflow di rilascio si rifiuta di pubblicare se il tag non combacia con
  `VERSION`, e avvia l'immagine appena costruita per verificare che `/health`
  risponda con la versione attesa prima di dare il verde.

### Modificato
- Le immagini prendono il nome di dove vengono pubblicate:
  `ghcr.io/yul-1/lan-manager-backend` e `-nginx`. Un nome solo per il pull, per
  la build locale e per `docker save`/`docker load` senza internet.

## [1.1.0] - 2026-09-06

### Added
- **Guided first run.** With no configuration, the dashboard now walks you
  through it instead of coming up empty: it proposes the networks to monitor by
  reading the host's interfaces, optionally takes your router, and writes the
  configuration for you. Nothing to edit by hand to get started.
- **English and Italian interface**, switchable from Settings with no reload.
  The choice is remembered per browser, so a phone and a desktop can differ.
  API error messages follow the browser's `Accept-Language`.
- `ui.default_language` sets the service-wide default for people who install on
  someone else's behalf. It never overrides a choice already made from the
  dashboard.
- `docker compose up --build` builds and starts the stack from a fresh clone.

### Fixed
- Address fields no longer suggest an example network of their own: the hint
  now comes from the subnets you configured, and is empty when there are none.

### Aggiunto
- **Primo avvio guidato.** Senza configurazione la dashboard ora guida invece di
  presentarsi vuota: propone le reti da monitorare leggendo le interfacce
  dell'host, prende il router se ne hai uno, e scrive la configurazione. Per
  partire non c'e' niente da modificare a mano.
- **Interfaccia in italiano e inglese**, si cambia da Impostazioni senza
  ricaricare. La scelta resta nel browser, quindi telefono e PC possono
  differire. I messaggi d'errore dell'API seguono l'`Accept-Language`.
- `ui.default_language` per la lingua predefinita del servizio, utile a chi
  installa per un'altra persona. Non scavalca mai una scelta gia' fatta.
- `docker compose up --build` costruisce e avvia lo stack da un clone pulito.

### Corretto
- I campi indirizzo non suggeriscono piu' una rete d'esempio propria: il
  suggerimento viene dalle subnet configurate, ed e' vuoto se non ce ne sono.

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

[Unreleased]: https://github.com/Yul-1/LAN-Manager/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Yul-1/LAN-Manager/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Yul-1/LAN-Manager/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/Yul-1/LAN-Manager/releases/tag/v0.1.0
