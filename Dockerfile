# syntax=docker/dockerfile:1
FROM python:3.11-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG MAINSAIL_VERSION=2.17.0
ARG MAINSAIL_SHA256=d010f4df25557d520ccdbb8e42fc381df2288e6a5c72d3838a5a2433c7a31d4e
ARG MAINSAIL_URL=https://github.com/mainsail-crew/mainsail/releases/download/v${MAINSAIL_VERSION}/mainsail.zip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx supervisor curl unzip ca-certificates && \
    rm -f /etc/nginx/sites-enabled/default && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Create directories explicitly to satisfy path checks
RUN mkdir -p /home/pi/printer_data/gcodes /home/pi/printer_data/config \
    && chmod 777 /home/pi/printer_data/gcodes /home/pi/printer_data/config

COPY . /app

RUN curl -L -o /tmp/mainsail.zip ${MAINSAIL_URL} \
    && echo "${MAINSAIL_SHA256}  /tmp/mainsail.zip" | sha256sum -c - \
    && rm -rf /usr/share/nginx/html/* \
    && unzip /tmp/mainsail.zip -d /tmp/mainsail \
    && shopt -s dotglob \
    && if [ -d /tmp/mainsail/mainsail ]; then mv /tmp/mainsail/mainsail/* /usr/share/nginx/html/; else mv /tmp/mainsail/* /usr/share/nginx/html/; fi \
    && rm -rf /tmp/mainsail /tmp/mainsail.zip

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 80

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
