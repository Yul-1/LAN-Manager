#!/usr/bin/env bash
# Deploy dell'archivio versionato su un host remoto (via SSH).
# Uso:  ./scripts/deploy.sh <user@host> [VERSION]
#
# Trasferisce l'archivio + docker-compose.yml, fa "docker load" e avvia lo stack.
# NON tocca config/ ne' i segreti sul server: la prima volta vanno predisposti
# a mano in ~/lanmng/config e ~/lanmng/ssh (vedi README).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    echo "Uso: $0 <user@host> [VERSION]" >&2
    exit 1
fi

VERSION="${2:-$(tr -d '[:space:]' < VERSION)}"
ARCHIVE="dist/lanmng-${VERSION}.tar.gz"
REMOTE_DIR="lanmng"

if [[ ! -f "$ARCHIVE" ]]; then
    echo "ERRORE: ${ARCHIVE} mancante. Esegui prima ./scripts/build-image.sh" >&2
    exit 1
fi

echo ">> Creo ~/${REMOTE_DIR} sul remoto"
ssh "$TARGET" "mkdir -p ~/${REMOTE_DIR}"

echo ">> Copio archivio + compose"
scp "$ARCHIVE" docker-compose.yml VERSION "${TARGET}:~/${REMOTE_DIR}/"

echo ">> Load + up (TAG=${VERSION})"
ssh "$TARGET" "cd ~/${REMOTE_DIR} && docker load < lanmng-${VERSION}.tar.gz && TAG=${VERSION} docker compose up -d"

echo
echo "Fatto. Dashboard:  http://${TARGET#*@}/"
echo "Stato:  ssh ${TARGET} 'cd ~/${REMOTE_DIR} && docker compose ps'"
