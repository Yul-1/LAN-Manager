# LANMng nginx — reverse proxy + frontend statico (baked nell'immagine)
# Build dal root del repo:  docker build -t lanmng-nginx -f nginx/Dockerfile .
# Base image fissata per digest (il tag e' mutabile): aggiornarla e' una scelta
# esplicita.  docker buildx imagetools inspect nginx:1.27-alpine
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

# Frontend incluso nell'immagine: export versionato self-contained.
COPY frontend/ /usr/share/nginx/html/
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
