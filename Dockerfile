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

FROM python:3.11-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG MAINSAIL_VERSION=2.17.0
ARG MAINSAIL_SHA256=d010f4df25557d520ccdbb8e42fc381df2288e6a5c72d3838a5a2433c7a31d4e
ARG MAINSAIL_URL=https://github.com/mainsail-crew/mainsail/releases/download/v${MAINSAIL_VERSION}/mainsail.zip
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

COPY . /app

RUN curl -L -o /tmp/mainsail.zip ${MAINSAIL_URL} \
    && if [ -n "${MAINSAIL_SHA256}" ]; then echo "${MAINSAIL_SHA256}  /tmp/mainsail.zip" | sha256sum -c -; fi \
    && rm -rf /usr/share/nginx/html/* \
    && unzip /tmp/mainsail.zip -d /tmp/mainsail \
    && shopt -s dotglob \
    && if [ -d /tmp/mainsail/mainsail ]; then mv /tmp/mainsail/mainsail/* /usr/share/nginx/html/; else mv /tmp/mainsail/* /usr/share/nginx/html/; fi \
    && curl -L -o /tmp/fluidd.zip ${FLUIDD_URL} \
    && if [ -n "${FLUIDD_SHA256}" ]; then echo "${FLUIDD_SHA256}  /tmp/fluidd.zip" | sha256sum -c -; fi \
    && rm -rf /usr/share/nginx/fluidd \
    && mkdir -p /usr/share/nginx/fluidd \
    && unzip /tmp/fluidd.zip -d /tmp/fluidd \
    && if [ -d /tmp/fluidd/fluidd ]; then mv /tmp/fluidd/fluidd/* /usr/share/nginx/fluidd/; else mv /tmp/fluidd/* /usr/share/nginx/fluidd/; fi \
    && rm -rf /tmp/mainsail /tmp/mainsail.zip \
    && rm -rf /tmp/fluidd /tmp/fluidd.zip

COPY --from=cnc-builder /out/ /usr/share/nginx/cnc/

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 80 81 82

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
