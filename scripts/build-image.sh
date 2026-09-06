#!/usr/bin/env bash
# Build delle immagini LANMng ed export in un singolo archivio versionato.
# Eseguire dal root del repo:  ./scripts/build-image.sh
set -euo pipefail

# Root del repo = cartella padre di questo script (a prescindere dalla cwd).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '[:space:]' < VERSION)"
if [[ -z "$VERSION" ]]; then
    echo "ERRORE: file VERSION vuoto." >&2
    exit 1
fi

# Cache-busting: allinea il ?v= degli asset in index.html al file VERSION. Senza
# questo un browser puo' continuare a usare la copia in cache dopo un deploy.
sed -i -E "s/\.(js|css)\?v=[^\"']*/.\1?v=${VERSION}/g" frontend/index.html
echo ">> Asset in index.html marcati come ?v=${VERSION}"

BACKEND_IMG="lanmng-backend"
NGINX_IMG="lanmng-nginx"
OUT="dist/lanmng-${VERSION}.tar.gz"

echo ">> Build ${BACKEND_IMG}:${VERSION}"
docker build -t "${BACKEND_IMG}:${VERSION}" -t "${BACKEND_IMG}:latest" -f backend/Dockerfile .

echo ">> Build ${NGINX_IMG}:${VERSION}"
docker build -t "${NGINX_IMG}:${VERSION}" -t "${NGINX_IMG}:latest" -f nginx/Dockerfile .

echo ">> Export -> ${OUT}"
mkdir -p dist
docker save "${BACKEND_IMG}:${VERSION}" "${NGINX_IMG}:${VERSION}" | gzip > "${OUT}"

SIZE="$(du -h "${OUT}" | cut -f1)"
echo
echo "OK. Archivio: ${OUT} (${SIZE})"
echo
echo "Passi successivi (deploy su un host remoto):"
echo "  scp ${OUT} docker-compose.yml VERSION <user>@<host>:~/lanmng/"
echo "  ssh <user>@<host>"
echo "  cd ~/lanmng && docker load < $(basename "${OUT}")"
echo "  TAG=${VERSION} docker compose up -d"
echo
echo "In alternativa:  ./scripts/deploy.sh <user>@<host>"
