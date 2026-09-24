# Changelog

All notable changes to LAN-Manager are documented here.
This file describes **what changes for people who run the app** — not the
development history.

Tutte le modifiche rilevanti di LAN-Manager sono documentate qui.
Questo file descrive **cosa cambia per chi usa l'app**, non la cronaca dello sviluppo.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

## [Unreleased]

## [1.2.0] - 2026-09-24

HTTPS for the setup that uses your own nginx. It changes the deployment: read the
upgrade notes before updating the vhost.

### Added
- **HTTPS for `deploy/lanmng.nginx.conf`.** The dashboard used to be served in
  plain HTTP on `:81`, so the login password and the session cookie crossed the
  LAN and any VPN in clear. One server block now serves it over TLS on `:443`,
  under its own `server_name`, and on `:81` by IP address, for clients that do not
  use your local DNS. Plain HTTP on `:80` (for that name) and on `:81` redirects to
  HTTPS. The session cookie gets the `Secure` flag automatically.
- `scripts/smoke.py --cacert <file>`: trust a private CA. TLS verification is
  always on; without the option the system trust store is used.

### Fixed
- `scripts/smoke.py` now checks that every script and stylesheet referenced by the
  index is served with a JavaScript/CSS content type. A frontend copied file by
  file could miss `i18n.js` and `i18n/`: nginx answered with `index.html`, the
  browser refused it and the dashboard stopped before the login page, while the
  smoke test still passed.

### Upgrade notes
- **Default setup (`docker-compose.yml`)**: nothing changes, it stays on HTTP.
- **Host that runs its own nginx (`deploy/docker-compose.host-nginx.yml`)**:
  1. Get a certificate whose SANs include the name you choose and the host
     address (for `:81`), signed by a CA your clients trust.
  2. Put `ssl_certificate`, `ssl_certificate_key` and the protocols in
     `/etc/nginx/snippets/lanmng-tls.conf`, or symlink an existing TLS snippet.
  3. Make the name resolve on your local DNS.
  4. Install the vhost from `deploy/lanmng.nginx.conf`, replacing every
     `lanmng.example.lan` with your name, then `nginx -t` and reload.
  5. Copy the whole `frontend/` folder (without `tests/`) to the web root, not a
     list of files.
  Bookmarks to `http://<host>:81` keep working: they are redirected.

### Aggiunto
- **HTTPS per `deploy/lanmng.nginx.conf`.** La dashboard era servita in HTTP in
  chiaro su `:81`: password di login e cookie di sessione passavano leggibili sulla
  LAN e su un'eventuale VPN. Ora un unico blocco server la serve in TLS su `:443`,
  con un suo `server_name`, e su `:81` per indirizzo IP, per i client che non usano
  il tuo DNS locale. L'HTTP in chiaro su `:80` (per quel nome) e su `:81` rimanda
  all'HTTPS. Il cookie di sessione prende da solo il flag `Secure`.
- `scripts/smoke.py --cacert <file>`: si fida di una CA privata. La verifica TLS e'
  sempre attiva; senza l'opzione vale il trust store del sistema.

### Corretto
- `scripts/smoke.py` controlla che ogni script e foglio di stile citato dall'index
  arrivi con un content type JavaScript/CSS. Un frontend copiato file per file
  poteva perdere `i18n.js` e `i18n/`: nginx rispondeva con `index.html`, il browser
  lo rifiutava e la dashboard si fermava prima del login, mentre lo smoke passava.

### Note di aggiornamento
- **Installazione standard (`docker-compose.yml`)**: non cambia nulla, resta in HTTP.
- **Host con un nginx suo (`deploy/docker-compose.host-nginx.yml`)**:
  1. Procurati un certificato con fra i SAN il nome scelto e l'indirizzo dell'host
     (per `:81`), firmato da una CA di cui i tuoi client si fidano.
  2. Metti `ssl_certificate`, `ssl_certificate_key` e i protocolli in
     `/etc/nginx/snippets/lanmng-tls.conf`, oppure fai un link a uno snippet TLS
     che hai gia'.
  3. Fai risolvere il nome sul tuo DNS locale.
  4. Installa il vhost da `deploy/lanmng.nginx.conf` sostituendo ogni
     `lanmng.example.lan` con il tuo nome, poi `nginx -t` e reload.
  5. Copia l'intera cartella `frontend/` (senza `tests/`) nella web root, non un
     elenco di file.
  I segnalibri a `http://<host>:81` continuano a funzionare: vengono rediretti.

## [1.1.3] - 2026-09-24

Security release for the connection between nginx and the backend. It changes the
deployment: read the upgrade notes before pulling the new image.

### Security
- **Any process on the host could reach the backend directly.** The backend listened
  on `127.0.0.1:8000` in host network mode and trusted the client address sent in
  `X-Forwarded-For`, so a local process could call the API and choose the address
  used by the login rate limit, the audit log and the LAN bypass. The backend now
  listens only on a unix socket, `/run/lanmng/backend.sock`, in a directory that
  only the backend and nginx can open. Port 8000 is gone.

### Changed
- Both bundled nginx configurations set `X-Forwarded-For` to the client address
  instead of appending to the value sent by the client.
- The container healthcheck queries `/health` over the socket
  (`backend/healthcheck.py`).

### Upgrade notes
- **Default setup (`docker-compose.yml`)**: nothing to do. The new `lanmng-run`
  volume is created on `docker compose up`.
- **Host that runs its own nginx (`deploy/docker-compose.host-nginx.yml`)**, before
  starting the new image:
  1. `sudo cp deploy/lanmng.tmpfiles.conf /etc/tmpfiles.d/lanmng.conf`, then
     `sudo systemd-tmpfiles --create`. If your nginx does not run as `www-data`,
     change the group in that file first.
  2. Update your vhost from `deploy/lanmng.nginx.conf` (new `upstream` block on the
     socket, `X-Forwarded-For $remote_addr`), then `nginx -t` and reload.
  Without step 1 the compose refuses to start and says the directory is missing.
- **Your own reverse proxy**: point it at `unix:/run/lanmng/backend.sock` and set
  `X-Forwarded-For` to the client address. Do not append (`$proxy_add_x_forwarded_for`):
  the backend trusts the first entry, so appending would let the client choose it.

### Sicurezza
- **Qualsiasi processo dell'host poteva raggiungere il backend direttamente.** Il
  backend ascoltava su `127.0.0.1:8000` in network mode host e si fidava dell'indirizzo
  client in `X-Forwarded-For`: un processo locale poteva chiamare l'API e scegliere
  l'indirizzo usato dal limite ai tentativi di login, dal log di audit e dal bypass LAN.
  Ora il backend ascolta solo su un unix socket, `/run/lanmng/backend.sock`, in una
  cartella che possono aprire solo il backend e nginx. La porta 8000 non c'e' piu'.

### Modificato
- Entrambe le configurazioni nginx incluse impostano `X-Forwarded-For` all'indirizzo
  del client invece di accodarlo al valore mandato dal client.
- L'healthcheck del container interroga `/health` sul socket (`backend/healthcheck.py`).

### Note di aggiornamento
- **Installazione standard (`docker-compose.yml`)**: niente da fare. Il nuovo volume
  `lanmng-run` si crea con `docker compose up`.
- **Host con un nginx suo (`deploy/docker-compose.host-nginx.yml`)**, prima di avviare
  la nuova immagine:
  1. `sudo cp deploy/lanmng.tmpfiles.conf /etc/tmpfiles.d/lanmng.conf`, poi
     `sudo systemd-tmpfiles --create`. Se il tuo nginx non gira come `www-data`,
     cambia prima il gruppo in quel file.
  2. Aggiorna il vhost da `deploy/lanmng.nginx.conf` (nuovo blocco `upstream` sul
     socket, `X-Forwarded-For $remote_addr`), poi `nginx -t` e reload.
  Senza il passo 1 il compose non parte e dice che la cartella manca.
- **Reverse proxy tuo**: puntalo a `unix:/run/lanmng/backend.sock` e imposta
  `X-Forwarded-For` all'indirizzo del client. Non accodare (`$proxy_add_x_forwarded_for`):
  il backend si fida della prima voce, e accodando la sceglierebbe il client.

## [1.1.2] - 2026-09-23

Security release. Upgrading is recommended for every installation, and required
reading for anyone who enabled `auth.bypass_lan` or `auth.method: none`.

### Security
- **Configuration and admin password could be changed without logging in.** With
  `auth.bypass_lan: true` or `auth.method: none`, any client on the local network
  could rewrite the configuration, the stored secrets and the admin password, and
  take over the dashboard at the next restart. These changes now always require an
  admin session. Setting the first password on a fresh install still works as before.
- **Container and VPN actions now always require an admin session.** Starting or
  stopping containers and reloading WireGuard were available to anyone the LAN
  bypass let in. Each action is now also written to the audit log.
- **The backend could be made to open SSH connections to arbitrary hosts.** The
  `journal:<host>` log source and systemd or Windows services added to the catalog
  accepted any address, and the backend connected to it with its own SSH credentials.
  Only hosts already configured for SSH are accepted now; existing catalog entries
  are left as they are.
- **The LAN bypass trusted every private address.** Segmented subnets, guest
  networks, VPN clients and carrier-grade NAT ranges all qualified. It now applies
  only to the new `auth.bypass_networks` list, which defaults to your `subnets`.
- **The session cookie is marked `Secure` when the dashboard is reached over
  HTTPS.** Plain HTTP deployments keep working.
- **`cors.allowed_origins: ["*"]` is rejected.** Combined with credentials it would
  have let any website call the API with the admin's session.

### Changed
- `PUT /api/config/secrets` answers 400 for an unknown secret id instead of a
  successful response that saved nothing.
- The example WireGuard subnet is now a documentation range (`203.0.113.0/24`).

### Upgrade notes
- Using `auth.bypass_lan`? If your `subnets` include a VPN or guest network, list
  only the trusted networks in `auth.bypass_networks`.
- Running your own reverse proxy in front of the dashboard? Forward
  `X-Forwarded-Proto` so the backend can mark the cookie `Secure`. The bundled nginx
  configurations already do.

### Sicurezza
- **Configurazione e password admin si potevano cambiare senza login.** Con
  `auth.bypass_lan: true` o `auth.method: none`, qualsiasi client della rete locale
  poteva riscrivere la configurazione, i segreti salvati e la password admin, e
  prendere il controllo della dashboard al riavvio successivo. Ora queste modifiche
  richiedono sempre una sessione admin. Impostare la prima password su
  un'installazione nuova funziona come prima.
- **Le azioni su container e VPN richiedono sempre una sessione admin.** Avviare o
  fermare container e ricaricare WireGuard era possibile per chiunque passasse dal
  bypass LAN. Ogni azione ora finisce anche nel registro di audit.
- **Il backend poteva essere indotto ad aprire connessioni SSH verso host
  arbitrari.** La sorgente di log `journal:<host>` e i servizi systemd o Windows
  aggiunti al catalogo accettavano qualsiasi indirizzo, e il backend vi si collegava
  con le proprie credenziali SSH. Ora si accettano solo host gia' configurati per
  SSH; le voci gia' presenti nel catalogo restano come sono.
- **Il bypass LAN si fidava di ogni indirizzo privato.** Valevano anche subnet
  segmentate, reti ospiti, client VPN e range CGNAT. Ora vale solo per il nuovo
  elenco `auth.bypass_networks`, che di default coincide con le tue `subnets`.
- **Il cookie di sessione e' marcato `Secure` quando si arriva alla dashboard in
  HTTPS.** Le installazioni in HTTP semplice continuano a funzionare.
- **`cors.allowed_origins: ["*"]` viene rifiutato.** Insieme alle credenziali avrebbe
  permesso a qualsiasi sito di chiamare le API con la sessione dell'admin.

### Modificato
- `PUT /api/config/secrets` risponde 400 per un id di segreto sconosciuto, invece di
  una risposta di successo che non salvava niente.
- La subnet WireGuard d'esempio e' ora un range di documentazione (`203.0.113.0/24`).

### Note di aggiornamento
- Usi `auth.bypass_lan`? Se le tue `subnets` comprendono una rete VPN o ospiti, elenca
  in `auth.bypass_networks` solo le reti fidate.
- Hai un tuo reverse proxy davanti alla dashboard? Inoltra `X-Forwarded-Proto`,
  cosi' il backend puo' marcare il cookie `Secure`. Le configurazioni nginx incluse
  lo fanno gia'.

## [1.1.1] - 2026-09-06

### Fixed
- **The stack now starts on a host whose port 80 is busy.** nginx used to loop on
  `bind() to 0.0.0.0:80 failed (98: Address in use)` and the dashboard was
  unreachable, with no way out short of editing the source: the port was written
  into the vhost, and `network_mode: host` leaves no port mapping to change. Set
  `LANMNG_HTTP_PORT` (default 80) in your `.env` or on the command line.
- **Local Docker monitoring works out of the box.** The root compose file never
  granted the host's `docker` group, so the non-root backend could not read
  `/var/run/docker.sock` and the Services page stayed empty with
  `[Errno 13] Permission denied`. It now uses `DOCKER_GID` (default 999, the
  usual value on Debian and Ubuntu), and if the gid is wrong the log says how to
  find yours instead of only reporting the error.

### Risolto
- **Lo stack parte anche dove la porta 80 e' occupata.** Prima nginx ripeteva
  all'infinito `bind() to 0.0.0.0:80 failed (98: Address in use)` e la dashboard
  era irraggiungibile, senza rimedio se non modificare il sorgente: la porta era
  scritta nel vhost e con `network_mode: host` non c'e' nessuna pubblicazione di
  porte da rimappare. Ora si sceglie con `LANMNG_HTTP_PORT` (default 80), nel
  `.env` o sulla riga di comando.
- **Il monitoraggio Docker locale funziona da subito.** Il compose della radice
  non concedeva il gruppo `docker` dell'host, quindi il backend (utente non-root)
  non poteva leggere `/var/run/docker.sock` e la pagina Servizi restava vuota con
  `[Errno 13] Permission denied`. Ora usa `DOCKER_GID` (default 999, il valore
  tipico su Debian e Ubuntu) e se il gid e' sbagliato il log dice come ricavare
  il proprio, invece di riportare solo l'errore.


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

[Unreleased]: https://github.com/Yul-1/LAN-Manager/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/Yul-1/LAN-Manager/compare/v1.1.3...v1.2.0
[1.1.3]: https://github.com/Yul-1/LAN-Manager/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/Yul-1/LAN-Manager/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/Yul-1/LAN-Manager/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Yul-1/LAN-Manager/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Yul-1/LAN-Manager/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/Yul-1/LAN-Manager/releases/tag/v0.1.0
