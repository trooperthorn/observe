# Multi-arch: python:3.12-slim-bookworm publishes amd64 and arm64, so the
# same file builds on the Pi 5 and on x86.
FROM python:3.12-slim-bookworm

# net-snmp CLI tools only. Not snmp-mibs-downloader: every query is numeric
# and runs with -m "" so no MIB files are needed.
#
# krb5-user provides `kinit` (used by observe/checks/windows.py to turn a
# keytab into a ticket for WinRM's kerberos transport) and libkrb5-3, both
# kept at runtime. gcc and libkrb5-dev are needed only to compile pykerberos
# against the krb5 headers and are removed again once pip install is done.
RUN apt-get update \
 && apt-get install -y --no-install-recommends snmp krb5-user libkrb5-3 gcc libkrb5-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove gcc libkrb5-dev \
 && rm -rf /var/lib/apt/lists/*
COPY observe ./observe
# Plugins are loaded only when listed under plugins: in the config, so installing them is harmless.
COPY plugins ./plugins
RUN pip install --no-cache-dir --no-deps ./plugins/pockethernet ./plugins/control ./plugins/unifi  && rm -rf ./plugins

# Fixed non-root UID/GID so volume ownership is predictable on the host.
RUN groupadd --gid 10001 observe \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin observe \
 && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

# The commit the image was built from, shown on the Updates page. docker-compose.yml passes
# OBSERVE_GIT_COMMIT from the environment or .env; the host helper passes `git rev-parse HEAD`.
# A plain `docker compose build` with neither set shows "unknown".
ARG OBSERVE_GIT_COMMIT=unknown
ENV OBSERVE_GIT_COMMIT=$OBSERVE_GIT_COMMIT
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"
ENTRYPOINT ["python", "-m", "observe"]
CMD ["--config", "/config/observe.yaml"]
