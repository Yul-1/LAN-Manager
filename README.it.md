# LAN-Manager

[English](README.md) · **Italiano**

Dashboard self-hosted per **gestire, monitorare e mappare** la LAN di casa o di un
piccolo ufficio. Gira in Docker, e' raggiungibile **solo dalla rete privata**, ed e'
pensata per essere configurata dalla sua stessa interfaccia web invece che
modificando file.

> **Stato: primo rilascio pubblico in preparazione.**
> Il codice viene reso generico e bilingue prima di arrivare qui. Fino alla versione
> `0.2.0` questo repository contiene solo la licenza e la documentazione.
> Vedi [CHANGELOG.md](CHANGELOG.md).

## Cosa fa

- **Dispositivi** — tutto quello che c'e' sulla LAN, acceso o spento, con una scheda
  di dettaglio per ciascuno (IP, MAC, produttore, sistema operativo ipotizzato,
  porte aperte, da quali sorgenti e' stato visto, ultimo avvistamento) che si
  aggiorna dal vivo finche' il dispositivo e' acceso. Si possono aggiungere,
  modificare, nascondere e rimuovere liberamente.
- **Discovery** — lease DHCP e tabella ARP dal router, piu' ping sweep, nmap,
  reverse DNS, informazioni raccolte via SSH e SNMP. Ogni sorgente e' un provider a
  se': se una fallisce, le altre continuano.
- **Mappa interattiva** — la rete disegnata come un grafo, con spostamento, zoom e
  nodi riposizionabili.
- **Traffico e WAN** — contatori e velocita' delle interfacce da un router OpenWrt,
  piu' lo stato del collegamento a internet (cablato, 4G, tethering).
- **Servizi** — per ogni servizio si sceglie **come** monitorarlo (container Docker,
  ping, porta TCP, stato HTTP, unit systemd) e **dove**, anche su altri host della
  rete. I container Docker compaiono da soli.
- **Risorse degli host** — CPU, RAM, dischi e temperature delle tue macchine, via SSH.
- **WireGuard** — stato dei peer, letto da dove WireGuard gira davvero, router compreso.
- **Logs** — syslog del router, log dell'applicazione, registro di audit e
  `journalctl` degli host della LAN, in un'unica vista filtrabile.
- **Strumenti di rete** — ping, traceroute, dig, scansione porte, verifica di una
  porta TCP, ARP ping, whois, ispezione HTTP e misura della velocita' della linea,
  con output che scorre mentre il comando gira.
- **Terminale SSH** nel browser, limitato a un elenco di host consentiti, con il
  registro di ogni comando eseguito.
- **Impostazioni** — tutta la configurazione e' modificabile dall'interfaccia, con
  validazione, backup automatici e segreti che non vengono mai rimandati al browser.

## Principi di progetto

1. **Solo dati reali.** Nessuna modalita' demo, nessun dato finto. Quando una
   sorgente non risponde, l'app degrada con un avviso esplicito e lo stato "non
   disponibile" — mai con numeri inventati.
2. **Niente scritto nel codice.** Nomi, indirizzi, subnet, host, servizi e
   credenziali stanno nella configurazione, mai nel codice. Se la rete cambia, si
   cambia la configurazione — possibilmente dall'interfaccia — e nient'altro.
3. **Solo rete privata.** Non e' fatta per stare su internet, e la documentazione
   continuera' a dirlo.
4. **Bilingue.** Inglese e italiano, con cambio al volo, su interfaccia, messaggi
   d'errore dell'API, configurazione di esempio e documentazione.

## Requisiti

- Docker e il plugin Docker Compose
- Una rete privata che si ha il diritto di scansionare
- Facoltativi, per le funzioni che li richiedono: un router OpenWrt raggiungibile
  via SSH, accesso SSH agli host di cui si vogliono le risorse, dispositivi SNMP

## Avvio rapido

> Disponibile dalla versione `0.2.0`.

```sh
git clone https://github.com/Yul-1/LAN-Manager.git
cd LAN-Manager
docker compose up --build
```

Poi si apre la dashboard e si segue la procedura di primo avvio: chiede una password
di amministratore, le subnet da scansionare e, se si vuole, il router e altri host
Docker. Scrive lei la configurazione: per partire non c'e' niente da modificare a mano.

## Configurazione

Tutto e' modificabile dalla pagina **Impostazioni**. Per chi preferisce i file, i
modelli stanno in `config/`:

| File | Cosa contiene |
|---|---|
| `config/config.example.yaml` | Configurazione principale, interamente commentata |
| `config/devices.example.yaml` | Catalogo dispositivi: nome e preferenze per indirizzo |
| `config/services.example.yaml` | Servizi da monitorare e con quale metodo |

Si copia il modello togliendo `.example` dal nome e si modifica. Ogni valore si puo'
anche sovrascrivere con una variabile d'ambiente con prefisso `LAN_`; le sezioni
annidate usano il doppio underscore, per esempio `LAN_ROUTER__HOST`.

I segreti (password del router, hash della password admin) vanno in
`config/secrets.env`, mai in `config.yaml` e mai nel repository. L'API dice soltanto
se un segreto e' impostato, mai quale sia.

## Sicurezza

- **Esporla solo sulla LAN.** Niente internet, e niente socket Docker su TCP.
- Il **login admin resta attivo** di serie. L'app non offre di proposito la
  scorciatoia "fidati di tutto quello che sta sulla rete locale": una LAN comprende
  anche subnet segmentate e client VPN.
- Se la configurazione e' permissiva, il backend lo scrive nel log all'avvio e la
  dashboard mostra un banner: una configurazione insicura non puo' passare inosservata.
- Il terminale SSH e gli strumenti di rete sono superficie sensibile: richiedono
  entrambi l'autenticazione e sono entrambi tracciati nel registro di audit.

Hai trovato un problema di sicurezza? Apri una issue senza allegare un exploit
funzionante, oppure contatta il manutentore in privato.

## Licenza

[MIT](LICENSE)
