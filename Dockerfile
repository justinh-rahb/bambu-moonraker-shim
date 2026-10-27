# syntax=docker/dockerfile:1
FROM node:20-alpine AS cnc-builder

ARG CNC_UI_REF=main
ARG CNC_UI_URL=https://github.com/justinh-rahb/moonraker-cnc/archive/refs/heads/${CNC_UI_REF}.zip
ARG CNC_UI_SHA256=

RUN apk add --no-cache curl unzip

RUN set -eux; \
    curl -L -o /tmp/cnc-ui.zip "${CNC_UI_URL}"; \
    if [ -n "${CNC_UI_SHA256}" ]; then echo "${CNC_UI_SHA256}  /tmp/cnc-ui.zip" | sha256sum -c -; fi; \
    mkdir -p /tmp/cnc-ui-src /out; \
    unzip /tmp/cnc-ui.zip -d /tmp/cnc-ui-src; \
    cnc_root="$(find /tmp/cnc-ui-src -mindepth 1 -maxdepth 1 -type d | head -n 1)"; \
    if [ -z "${cnc_root}" ]; then echo "Unable to locate CNC UI root in archive"; exit 1; fi; \
    if [ -f "${cnc_root}/dist/index.html" ]; then \
      cp -R "${cnc_root}/dist/." /out/; \
      exit 0; \
    fi; \
    if [ -f "${cnc_root}/build/index.html" ]; then \
      cp -R "${cnc_root}/build/." /out/; \
      exit 0; \
    fi; \
    if [ ! -f "${cnc_root}/package.json" ]; then \
      echo "No dist/build assets or package.json found in ${CNC_UI_URL}"; \
      exit 1; \
    fi; \
    cd "${cnc_root}"; \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi; \
    npm run build; \
    if [ -f dist/index.html ]; then \
      cp -R dist/. /out/; \
    elif [ -f build/index.html ]; then \
      cp -R build/. /out/; \
    else \
      echo "Build completed but no dist/build index.html found for CNC UI"; \
      exit 1; \
    fi

FROM node:22-alpine AS mainsail-builder

ARG MAINSAIL_VERSION=2.19.0
ARG MAINSAIL_SOURCE_URL=https://github.com/mainsail-crew/mainsail/archive/refs/tags/v${MAINSAIL_VERSION}.tar.gz
ARG MAINSAIL_SOURCE_SHA256=19908bcd07804328877cf1843c2436a5c66f66e7c417972a7cd262304057cbd6
ENV CYPRESS_INSTALL_BINARY=0
RUN apk add --no-cache curl patch
WORKDIR /mainsail
RUN curl -fL "${MAINSAIL_SOURCE_URL}" -o /tmp/mainsail.tar.gz \
    && echo "${MAINSAIL_SOURCE_SHA256}  /tmp/mainsail.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/mainsail.tar.gz --strip-components=1 \
    && rm /tmp/mainsail.tar.gz
RUN npm ci
COPY docker/mainsail/3mf-support.patch /tmp/3mf-support.patch
RUN patch -p1 < /tmp/3mf-support.patch && npx vite build

FROM python:3.11-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG FLUIDD_URL=https://github.com/fluidd-core/fluidd/releases/latest/download/fluidd.zip
ARG FLUIDD_SHA256=
ARG CNC_UI_REF=main
ARG CNC_UI_URL=https://github.com/justinh-rahb/moonraker-cnc/archive/refs/heads/${CNC_UI_REF}.zip
ARG CNC_UI_SHA256=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx supervisor curl unzip ca-certificates && \
    rm -f /etc/nginx/sites-enabled/default && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN rm -rf /usr/share/nginx/html/* \
    && shopt -s dotglob \
    && curl -L -o /tmp/fluidd.zip ${FLUIDD_URL} \
    && if [ -n "${FLUIDD_SHA256}" ]; then echo "${FLUIDD_SHA256}  /tmp/fluidd.zip" | sha256sum -c -; fi \
    && rm -rf /usr/share/nginx/fluidd \
    && mkdir -p /usr/share/nginx/fluidd \
    && unzip /tmp/fluidd.zip -d /tmp/fluidd \
    && if [ -d /tmp/fluidd/fluidd ]; then mv /tmp/fluidd/fluidd/* /usr/share/nginx/fluidd/; else mv /tmp/fluidd/* /usr/share/nginx/fluidd/; fi \
    && rm -rf /tmp/fluidd /tmp/fluidd.zip

COPY . /app

COPY --from=mainsail-builder /mainsail/dist/ /usr/share/nginx/html/
COPY --from=cnc-builder /out/ /usr/share/nginx/cnc/

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 80 81 82

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
