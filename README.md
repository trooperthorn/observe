# watchpost

A small, self-hosted availability monitor for a homelab, built to cover what
SolarWinds ipMonitor was good at: agentless up/warn/down checks, confirmation
before paging, a one-glance dashboard, and alerts, in a single container.

It is an independent implementation built from publicly described behaviour.
It shares no code with, and is not affiliated with, any SolarWinds product.

## What it checks

| Type | What it proves | Value used for thresholds |
|---|---|---|
| `ping` | ICMP echo reply | average RTT, ms (any loss is WARN) |
| `tcp` | port accepts a connection, optional banner match | connect time, ms |
| `http` | status code, optional body text, private CA support | response time, ms |
| `dns` | record resolves, optional expected answers | query time, ms |
| `tls_cert` | chain and hostname validate, days to expiry | days remaining (default WARN 21, FAIL 7) |
| `snmp` | `oid`, `uptime`, `interface`, `cpu`, `memory` over v2c or v3 | OID value, days up, % utilization, % CPU, % RAM |
| `winrm` | `service` state, `cpu`, `memory`, `disk`, or a `powershell` script | %, or the script's number |
| `wmi` | a WQL query over WinRM with `first/sum/avg/max/min/count` | the aggregate |
| `mqtt` | broker connect/auth, or a topic's payload | payload, if `numeric: true` |
| `linux` | over SSH: `cpu`, `memory`, `disk`, `load`, `uptime`, systemd `service`, or a `command` | %, days, or the command's number |
| `docker` | over SSH: one `container` (state, health, restart count) or a host `summary` | problem count (summary) |
| `truenas` | JSON-RPC WebSocket API: all `pools`, one `pool`, or active `alerts` | % used, alert count |
| `proxmox` | REST API with a token: `node`, `node_cpu`, `node_memory`, `guest` (VM or CT), `storage` | days up, % |
| `vsphere` | ESXi or vCenter via pyVmomi: `host`, `host_cpu`, `host_memory`, `datastore`, `vm` | days up, % |
| `homeassistant` | REST API: `api`, one `entity` (state or number), `unavailable` entity count, pending `updates` | entity value, counts |
| `unifi_network` | Integration API: `devices` online summary, one `device`, `device_cpu`, `device_memory`, `firmware` updates | %, counts |
| `unifi_protect` | Integration API: `cameras` connected summary, one `camera`, `info` | not-connected count |
| `technitium` | HTTP API: `stats` (SERVFAIL rate over the last hour or day), `update` available | % SERVFAIL |

`config.example.yaml` has one worked example of every type.

## How state works

Each poll returns OK, WARN, or FAIL. A monitor becomes DOWN only after
`failures_to_down` consecutive FAILs (default 3), WARN after that many
consecutive WARN-or-worse results, and UP after `recoveries_to_up`
consecutive OKs. Alerts fire on those transitions, not on individual polls.
The first transition after a restart (PENDING to UP) is recorded but not
alerted.

## Quick start (details below) (Docker on Debian or a Pi)

```sh
git clone <this repo> watchpost && cd watchpost
mkdir -p config data secrets
cp config.example.yaml config/watchpost.yaml   # then edit it
cp .env.example .env                            # then fill in secrets
sudo chown 10001:10001 data                     # the container runs as UID 10001
docker compose run --rm watchpost --config /config/watchpost.yaml --validate
docker compose run --rm watchpost --config /config/watchpost.yaml --once
docker compose up -d
```

`--validate` checks the config and every secret reference, then exits.
`--once` polls every monitor a single time and prints the result, which is the
fastest way to prove credentials and firewall paths before running the service.
Add `--only <slug>` to poll one monitor. The exit code is 0, 1 (a WARN), or
2 (a FAIL), so it also works from cron or a script.

Dashboard: `http://<host>:8080/`. Also `/api/monitors`,
`/api/monitors/<slug>/history?hours=24`, `/api/groups`, `/api/forecasts`,
`/api/events`, `/metrics`
(Prometheus text format), and `/healthz`.

## Target preparation

**SNMP.** Prefer v3 authPriv. The `cpu` and `memory` modes read
HOST-RESOURCES-MIB (`hrProcessorLoad`, `hrStorageRam`). Some agents do not
populate these; the check then says so instead of reporting zero. On Linux
net-snmp, `hrStorageRam` "used" includes page cache, so memory reads higher
than real pressure.

**Windows (WinRM and WMI).** Both use WS-Management on 5986 with TLS
validation on by default. Point `ca_bundle` at your internal root if the
listener certificate is from your own CA. The monitoring account needs
remote WinRM access and read access to the CIM classes you query; it does
not need to be an administrator if you delegate those rights. For
`transport: certificate`, map a client certificate to the account on the
target (WinRM certificate mapping) and mount the PEM and key under
`./secrets`. CredSSP is not supported.

`transport: kerberos` is for domains where NTLM is disabled or being phased
out (Microsoft has been pushing this for years). It authenticates as a
regular AD service account, never a gMSA: a gMSA's password is retrievable
only by domain-joined Windows computer accounts on its
`PrincipalsAllowedToRetrieveManagedPassword` list, which a container has no
way to be, so there is no such thing as "watchpost running as a gMSA".

1. On a DC, create the service account and its keytab, e.g.:
   ```powershell
   ktpass -princ watchpost/monitor@LAB.EXAMPLE.COM -mapuser LAB\svc-watchpost `
     -crypto AES256-SHA1 -ptype KRB5_NT_PRINCIPAL -out watchpost.keytab
   ```
   (`msktutil` is the equivalent tool if you manage the account from Linux.)
   Use AES256, not RC4 — modern DCs support it and RC4 is what's actually
   being deprecated alongside NTLM.
2. Put `watchpost.keytab` under `./secrets` (never in `./config`, which the
   container mounts read-only for config, not secrets, and which you may put
   in git).
3. Write a `krb5.conf` for your realm (`[libdefaults] default_realm =
   LAB.EXAMPLE.COM`, `[realms]` with your KDC, `[domain_realm]` mapping your
   DNS domain to the realm) and mount it read-only at `/etc/krb5.conf` (see
   `docker-compose.yml`).
4. In the credential, set `principal` to the keytab's principal and
   `keytab_path` to where the keytab lands inside the container
   (`/run/secrets/watchpost.keytab`), and reference it from a monitor as
   usual.
5. `kinit -kt` runs automatically before each poll (a ticket is cached for a
   few hours, not re-acquired every time); nothing needs to be pre-authenticated
   on the host. Time skew between the container and the KDC must stay under
   about 5 minutes or Kerberos rejects the ticket outright — make sure the
   Docker host's clock is NTP-synced.
6. All Kerberos WinRM/WMI calls are serialized process-wide (unlike NTLM and
   certificate transports, which run in parallel): the ticket cache is
   selected via the `KRB5CCNAME` environment variable, which the underlying
   GSSAPI library reads per-process, not per-thread, so concurrent Kerberos
   calls for different accounts could otherwise race. This only limits
   Kerberos-authenticated Windows polling throughput, not other check types.

**Linux (SSH).** Agentless: reads /proc and runs df and systemctl, which
every distribution has. Create a dedicated account with a key (ed25519) and
no password; it needs no sudo for the `linux` checks. Host keys are always
verified against `known_hosts` (default `/config/known_hosts`); a monitor has
no option to skip that. Memory is `1 - MemAvailable/MemTotal`, which excludes
reclaimable cache, so it reads lower and truer than SNMP on the same box.

**Docker (SSH).** Runs the docker CLI on the host: `container` mode reads only
the container's State and RestartCount (not its full config, which includes
environment variables that often hold secrets), fails on exited or unhealthy,
and WARNs when the restart count grows between polls. `summary` mode counts
containers that are unhealthy or stopped despite an `always`/`unless-stopped`
restart policy; a stopped one-shot container with `restart: no` is not a
problem. The account needs Docker access, which is root-equivalent; see
THREAT-MODEL.md for a sudo rule that narrows it.

**TrueNAS.** Uses the JSON-RPC 2.0 WebSocket API at `wss://host/api/current`,
which TrueNAS documents as the supported API from 25.04; the REST API is
deprecated in 25.04 and removed in 26, so it is not used. Authentication is
`auth.login_ex` with a user-linked API key (`API_KEY_PLAIN`). Create a
dedicated user with the READONLY_ADMIN role and generate its key. `pools`
fails on any pool that is not healthy and reports the fullest; `pool` tracks
one pool's usage (forecast-ready); `alerts` counts active, non-dismissed
alerts at `min_alert_level` or above and fails on CRITICAL. TrueNAS CORE 13
has no JSON-RPC API and is not supported (use SNMP or SSH there).

**Proxmox VE.** One call to `/api2/json/cluster/resources` with a token in
the `Authorization: PVEAPIToken=...` header covers a single node or a
cluster. Create a user, grant it PVEAuditor at `/`, and create a token. If
the token has privilege separation enabled it needs its own PVEAuditor ACL;
a token that authenticates but sees nothing is reported as exactly that.
`guest` accepts a name or VMID, fails when stopped or when its HA state is
`error`, and reports CPU.

**VMware vSphere.** Works against a standalone ESXi host or vCenter (set
`entity` to pick a host when several are visible). `host` fails when the host
is disconnected or has a red alarm, WARNs on yellow or maintenance mode; `vm`
fails when not powered on or when the guest heartbeat is red (guest OS
hung), and notes when VMware Tools is not reporting. Each poll logs in and
out, since ESXi caps concurrent sessions; use intervals of a minute or more.

**Home Assistant.** REST API with a long-lived token (`Authorization: Bearer`).
Create a dedicated non-admin user and generate the token from its profile.
`api` confirms the API answers "API running."; `entity` fails on
`unavailable` or `unknown`, can require specific states with `expect`, and
applies thresholds to numeric states (so any HA sensor, from a NAS
temperature to a Phyn flow reading, becomes a watchpost monitor);
`unavailable` counts unavailable entities with `domains` and fnmatch
`ignore` filters for the ones you know are dead; `updates` counts `update.*`
entities that are on. HA serves plain HTTP unless configured otherwise: set
`https: false` for that, and read THREAT-MODEL.md on what it means.

**UniFi Network and Protect.** Both use the console's Integration APIs with
an `X-API-KEY` header from Settings > Control Plane > Integrations; one key
serves both. Network list endpoints are paged, and every page is read.
`devices` fails when any adopted device is not ONLINE (use `ignore` for
stale records); `device_cpu` and `device_memory` read
`/statistics/latest`. For a standalone (non-UniFi OS) controller set
`base_path: /integration/v1`. Protect's Integration API reports whether a
camera is connected, not whether it is recording, so these checks do not
claim anything about recording.

**Technitium DNS.** `stats` reads the dashboard counters and reports the
SERVFAIL share of queries over `LastHour` or `LastDay`, with blocked and
client counts in the message; `update` WARNs when a new version is out. The
token goes in an `Authorization: Bearer` header, which current Technitium
documents; `token_in_query: true` falls back to the older `?token=` form,
which puts the token in logs. Pair it with a plain `dns` monitor pointed at
the server to prove it actually answers.

**Self-signed appliance certificates.** Proxmox, ESXi, and TrueNAS ship with
self-signed certificates. Either issue them certificates from your CA, or pin
each one: put the device's certificate in a PEM file and set it as that
monitor's `ca_bundle`. Partial-chain verification is enabled, so a single
self-signed leaf works as the trust anchor while the hostname check still
applies. `verify_tls: false` also works, and is labelled for what it is.

**MQTT.** Without a `topic`, the check only connects. With a `topic`, a
retained message satisfies it immediately, which proves the broker holds a
value, not that the publisher is alive. For liveness, watch an availability
or LWT topic with `expect: online`.

## Discovery

```sh
docker compose run --rm watchpost --config /config/watchpost.yaml --discover \
  --target 192.0.2.0/24 --target 198.51.100.10-40 --target dc01.lab.example \
  --credential snmp-v3-core --credential win-monitor \
  --out /data/proposals.yaml --report /data/discovery.json
```

Targets can be CIDRs, ranges (`192.0.2.10-40` or `192.0.2.10-192.0.2.40`),
single IPs, or hostnames, on the command line or under `discovery.targets`.
Credentials are names from `credentials:` and are tried in order; nothing
else is ever sent. Discovery writes a proposal file and changes nothing: you
copy what you want into `watchpost.yaml`, then `--validate`, `--once`, restart.

**From Active Directory.** With `discovery.directory` configured, discovery
first queries a domain controller over LDAPS for computer accounts, then
scans each by its DNS name alongside any other targets. Disabled accounts,
accounts with no logon in `stale_days`, and (optionally) operating systems
not in `os_include` are skipped, each with its reason in `--report`.
Computers that answer no probe are listed at the end of the proposals, which
doubles as a stale-object report. `--no-directory` skips the query for one
run. Because AD hands over DNS names, Windows hosts found this way pass the
certificate checks that WinRM discovery requires.

Per address it runs an ICMP echo, a TCP connect to `tcp_ports`, and a PTR
lookup, then:

| Finding | Proposed monitors |
|---|---|
| ICMP reply | `ping` |
| SNMP answers a credential | `uptime`; `cpu` and `memory` if HOST-RESOURCES tables exist; `interface` for each physical link that is up, fastest first, up to `max_interfaces` (utilization 70/90, forecast on) |
| WinRM 5986 with a validating certificate and a working credential | `cpu`, `memory`, `disk` per fixed drive (forecast on), and `service` for role services set to Automatic: NTDS, Netlogon, Kdc, ADWS, DNS, DFSR, DHCPServer, CertSvc, W3SVC, MSSQLSERVER, SQLSERVERAGENT, vmms, WSUSService |
| TLS on a port | `tls_cert` hourly, plus `http` with the observed status code |
| Plain HTTP | `http` with the observed status code |
| SSH with a working `ssh` credential | `linux` CPU, memory, uptime, `disk` per real mount (forecast on), `service` for running role units (nginx, postgresql, docker, smbd, named, unbound, pihole-FTL, mosquitto, k3s, and similar); if Docker is usable, a `docker` summary and a `container` monitor per running container (up to `max_containers`) |
| SSH banner only / RDP | `tcp` with `expect: SSH-2.0` / `tcp` 3389 |
| MQTT broker | `mqtt` connect check, anonymous or with the credential that worked |
| Proxmox API with a working token | `node`, `node_cpu`, `node_memory` per node; `storage` per active store (shared ones once); `guest` per running VM and container, each with `depends_on` its node |
| vSphere (fingerprinted unauthenticated via `/sdk/vimServiceVersions.xml`) with a working login | `host`, `host_cpu`, `host_memory` per host; `datastore` per accessible datastore; `vm` per powered-on VM, each with `depends_on` its host |
| TrueNAS API with a working key | `pools`, `alerts`, and `pool` per pool |
| Home Assistant on 8123 with a working token | `api`, `unavailable`, `updates` |
| UniFi console with a working key | `devices` (devices offline at discovery time go into its `ignore`), `firmware`, and `device`, `device_cpu`, `device_memory` per online device; if Protect answers, `cameras` and a `camera` per camera |
| Technitium on 5380 with a working token | `stats` and `update` |

Every host block in the output starts with comments giving the evidence
(sysDescr, OS, certificate subject and validity, notes such as "credentials
not sent"). Proposals that match an existing monitor are skipped, and every
proposal is validated against the config schema before it is written.

Discovery infers `depends_on` only where the source states the relationship
(guests on Proxmox nodes and ESXi hosts). Network topology would need LLDP or
CDP neighbour tables and is not inferred; nor are DNS monitors or anything
beyond conservative default thresholds.

**Naming matters for Windows and TLS.** Certificates are issued to names, so
WinRM and certificate validation only succeed when the host has a PTR record
or you list it by hostname. A bare IP with no PTR gets its TLS findings
marked not valid and WinRM credentials are not sent to it.

**SSH host keys during discovery.** A host whose key is already in
`ssh_known_hosts` is verified as usual. For a host that is not listed, and
only with `ssh_trust_on_first_use: true` (the default), discovery connects
with **key-based credentials only**, records the key, and lists it at the end
of the proposals (`--known-hosts-out` writes them to a file). Password
credentials are never offered to an unverified host. A key that differs from
a listed one, or a listed entry that can not be parsed, stops SSH for that
host and is flagged in its notes. Verify fingerprints out of band before
appending them to your known_hosts.

**API credentials and TLS during discovery.** Proxmox tokens, TrueNAS keys,
vSphere passwords, UniFi keys, and Home Assistant and Technitium tokens are
only sent after the endpoint's certificate
validates (`api_require_valid_tls`, default on). For a self-signed device that
means the first discovery run sends nothing, lists the endpoint with its
SHA-256 fingerprint, and `--certs-out DIR` saves its certificate. Verify the
fingerprint on the device's console, add the PEM to `discovery.ca_bundle`
(and to the monitors' `ca_bundle`), and run discovery again. Discovery infers
`depends_on` for guests because the hypervisor reports where each one runs.
Home Assistant and Technitium usually serve plain HTTP; discovery will not
send their tokens there unless you set `api_require_valid_tls: false`,
which you might reasonably do for a scan scoped to a management VLAN.

**Scale.** A /24 of mostly empty space takes about 15 seconds at the default
concurrency (measured in a container; your network's latency will dominate).
`max_hosts` (default 4096) is checked before any list is built, so a
mistyped `/8` fails immediately.

## Status rollup

**Dependencies.** `depends_on: [<monitor name>]` places a monitor behind
another (a server behind a switch, a VM behind its host). When a monitor
fails, any parent that is failing but not yet confirmed, or has never been
polled, is polled immediately to settle the root cause. If an ancestor is
DOWN, the monitor shows **UNREACHABLE via <root cause>**, its alert is
suppressed, and the event log records the suppression. When the parent
recovers, its dependents are re-polled at once; any still failing on their
own are alerted then. An UP alert is sent only if its problem alert was.

**Groups.** Each `group` shows the worst effective state of its members.
`critical: false` lets a member degrade its group to WARN but not DOWN.
Group state is on the dashboard, in `/api/groups`, and in `/metrics` as
`watchpost_group_state`.

## Capacity forecasting

Set `forecast: true` on any monitor that has numeric values and thresholds
(disk, memory, interface utilization, UPS charge). Once an hour, and on
demand at `/api/forecasts?refresh=true`, watchpost averages the last
`lookback_days` of values into hourly buckets, fits a least-squares line, and
projects when it crosses the warn and crit thresholds. Results appear on
each monitor card, in a "Capacity outlook" list sorted by soonest crossing,
and in `/metrics` as `watchpost_forecast_seconds{level="warn|crit"}`.

Every projection carries its r-squared. Below `min_r2` it is labelled low
confidence rather than hidden. Flat or receding trends, too little history,
and crossings beyond `horizon_days` return a status and reason, never a
date. A straight line ignores daily and weekly cycles; treat projections for
seasonal series with that in mind. Forecasts inform; they do not alert.

The methods and their public sources are recorded in `docs/PRIOR-ART.md`.

## Alerts

`ntfy`, `webhook` (JSON POST), `smtp`, and `mqtt`. The MQTT target publishes a
retained `watchpost/<slug>/state` (`up`, `warn`, `down`) and a non-retained
JSON `watchpost/<slug>/event`, which Home Assistant can consume with an MQTT
binary sensor. Each target has `notify_on` (default `[down, up]`) and each
monitor can restrict itself to named targets with `alerts:`. Failed deliveries
are retried once, then shown in the dashboard footer.

## Direction: no longer read-only

The owner reversed the earlier decision that watchpost is read-only by
design. It is becoming the single monitoring UI and, in a later phase, the
control plane, replacing the hostwatch hub and web view. The first phase adds
ingest from hostwatch agents, logins, an admin role, and an audit log, and
builds no action that changes a host. The design and its limits are in
`docs/ARCHITECTURE.md`, and the new risks are in `THREAT-MODEL.md`. Parts of
this README describe the read-only behavior of the current release; they are
updated as each phase lands.

The SQLite store is now versioned. On startup watchpost applies any missing
additive migrations, keeps all existing history, and refuses to open a
database written by a newer version. `server.retention_days` governs poll
results and host samples, and the new `server.audit_retention_days` (default
365) governs the audit log independently. The new tables for hosts, keys,
users, sessions and audit stay empty until the ingest and login work lands.

## Not implemented

Network discovery, automated remediation actions, maps, native DCOM WMI,
Holt-Winters seasonal forecasting, alerts on forecasts,
SNMP traps, maintenance windows, and editing monitors from the UI (the YAML stays the
source of truth for polled monitors). See `docs/VERIFICATION.md` for what has and
has not been tested against real systems.

## Development

```sh
pip install -r requirements-dev.txt
tests/services.sh        # loopback snmpd and mosquitto for integration tests
python -m pytest -q -rs
```

Set `WATCHPOST_REQUIRE_SERVICES=1` to make missing test services a failure
rather than a skip; CI does this.
