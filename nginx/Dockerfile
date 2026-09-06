# LANMng nginx — reverse proxy + frontend statico (baked nell'immagine)
# Build dal root del repo:  docker build -t lanmng-nginx -f nginx/Dockerfile .
# Base image fissata per digest (il tag e' mutabile): aggiornarla e' una scelta
# esplicita.  docker buildx imagetools inspect nginx:1.27-alpine
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

# Il vhost di serie va via: il nostro arriva come template ed e' l'entrypoint a
# generarlo in conf.d all'avvio (vedi sotto). Tenerlo vorrebbe dire avere
# nell'immagine un secondo server sulla 80, che se l'espansione non avvenisse
# servirebbe la pagina di benvenuto di nginx invece di far fallire l'avvio.
RUN rm -f /etc/nginx/conf.d/default.conf

# La porta di ascolto e' un parametro, non una costante: il container gira in
# network_mode host, quindi su un host dove la 80 e' gia' occupata nginx non
# partirebbe affatto e non ci sarebbe modo di spostarlo senza ricostruire
# l'immagine. L'entrypoint ufficiale espande i template di /etc/nginx/templates
# in /etc/nginx/conf.d all'avvio (20-envsubst-on-templates.sh).
#
# Il filtro limita la sostituzione alle variabili LANMNG_*: senza, envsubst
# lavorerebbe su tutto l'ambiente del container e una variabile omonima di una
# di nginx svuoterebbe la direttiva che la usa.
ENV LANMNG_HTTP_PORT=80 \
    NGINX_ENVSUBST_FILTER=^LANMNG_

# Frontend incluso nell'immagine: export versionato self-contained.
COPY frontend/ /usr/share/nginx/html/
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
