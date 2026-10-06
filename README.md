# Observe

A small, self-hosted availability monitor for a homelab, built to cover what
SolarWinds ipMonitor was good at: agentless up/warn/down checks, confirmation
before paging, a one-glance dashboard, and alerts, in a single container.

It is an independent implementation built from publicly described behaviour.
It shares no code with, and is not affiliated with, any SolarWinds product.
The source lives at https://github.com/trooperthorn/observe.

## What it checks

| Type | What it proves | Value used for thresholds |
|---|---|---|
| `ping` | ICMP echo reply | average RTT, ms (any loss is WARN) |
| `tcp` | port accepts a connection, optional banner match | connect time, ms |
| `http` | status code, optional body text, private CA support | response time, ms |
| `dns` | record resolves, optional expected answers | query time, ms |
| `tls_cert` | chain and hostname validate, days to expiry | days remaining (default WARN 21, FAIL 7) |
| `snmp` | `oid`, `uptime`, `interface`, `cpu`, `memory`, `storage` over v2c or v3; with `host_name` the cpu, memory, storage and interface readings also appear on that host's page | OID value, days up, % utilization, % CPU, % RAM, % of the fullest disk |
| `winrm` | `service` state, `cpu`, `memory`, `disk`, or a `powershell` script | %, or the script's number |
| `wmi` | a WQL query over WinRM with `first/sum/avg/max/min/count` | the aggregate |
| `mqtt` | broker connect/auth, or a topic's payload | payload, if `numeric: true` |
| `linux` | over SSH: `cpu`, `memory`, `disk`, `load`, `uptime`, systemd `service`, or a `command` | %, days, or the command's number |
| `docker` | over SSH: one `container` (state, health, restart count) or a host `summary` | problem count (summary) |
| `truenas` | JSON-RPC WebSocket API: all `pools`, one `pool`, or active `alerts` | % used, alert count |
| `proxmox` | REST API with a token: `node`, `node_cpu`, `node_memory`, `guest` (VM or CT), `storage` | days up, % |
| `vsphere` | ESXi or vCenter via pyVmomi: `host`, `host_cpu`, `host_memory`, `datastore`, `vm` | days up, % |
| `homeassistant` | REST API: `api`, one `entity` (state or number), `unavailable` entity count, pending `updates`, `host` (versions, run state, updates, entity counts, hassio Core, Supervisor and add-on CPU and memory, disk sensors and HA SOC sensors shown as a Hosts page, read every 300 s; a default install with none of the opt-in sensors grades Good). Every mode refuses a redirect and a reply over 16 MB | entity value, counts |
| `unifi_network` | Integration API: `devices` online summary, one `device`, `device_cpu`, `device_memory`, `firmware` updates, `ports` per-port speed, state and PoE state of one switch | %, counts |
| `unifi_protect` | Integration API: `cameras` connected summary, one `camera`, `info` | not-connected count |
| `technitium` | HTTP API: `stats` (SERVFAIL rate over the last hour or day), `update` available | % SERVFAIL |
| `pushed_host` | No poll: reads the newest batch a hostwatch agent pushed; Good, Warning or Critical per component, plus staleness | seconds since last batch |

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
git clone <this repo> observe && cd observe
mkdir -p config data secrets
cp config.example.yaml config/observe.yaml   # then edit it
cp .env.example .env                            # then fill in secrets
sudo chown 10001:10001 data                     # the container runs as UID 10001
docker compose run --rm observe --config /config/observe.yaml --validate
docker compose run --rm observe --config /config/observe.yaml --once
docker compose up -d
```

Upgrading a deployment that still uses the old name watchpost: see
`docs/UPGRADING-FROM-WATCHPOST.md`. The legacy environment variables, config path and
database path are still read with a warning.

`--validate` checks the config and every secret reference, then exits.
`--once` polls every monitor a single time and prints the result, which is the
fastest way to prove credentials and firewall paths before running the service.
Add `--only <slug>` to poll one monitor. The exit code is 0, 1 (a WARN), or
2 (a FAIL), so it also works from cron or a script.

Setting up a host: sign in at `http://<host>:8080/login`, open `/hosts/new` (Hosts, Add host in the
navigation) and follow the wizard. It asks for the host name and platform, whether to install the
agent and the control daemon, and the control allowlist. It then shows one install command headed with
the host name, which you copy and run once on that machine. The install script refuses to run on a
machine whose host name differs and on the Observe host itself, and the page shows live progress
until the host reports data. You do not create keys or write `control.toml` by hand. See "Adding a
host" below.

Dashboard: `http://<host>:8080/`. The Customize button lets each signed-in user reorder and hide
the group cards and the capacity, findings and events cards with Up, Down and Hide controls; the
layout is saved per user on the server (`GET`, `PUT` and `DELETE /api/ui/layout/dashboard`), so it
follows the user between devices, and hiding a card never deletes data. Also `/api/monitors`,
`/api/monitors/<slug>/history?hours=24`, `/api/groups`, `/api/forecasts`,
`/api/events`, `/metrics`
(Prometheus text format), and `/healthz`.

## Target preparation

**SNMP.** Prefer v3 authPriv. The `cpu` and `memory` modes read
HOST-RESOURCES-MIB (`hrProcessorLoad`, `hrStorageRam`). Some agents do not
populate these; the check then says so instead of reporting zero. On Linux
net-snmp, `hrStorageRam` "used" includes page cache, so memory reads higher
than real pressure. The `storage` mode reads the fixed disks of `hrStorageTable` and
computes bytes as allocation units times size (and times used), never the raw count,
because net-snmp may raise the unit for a large filesystem; `mount` limits it to one
`hrStorageDescr`. For the Home Assistant host, point the monitors at the HA SOC Probe
add-on over SNMPv3 and give them the same `host_name` as the `homeassistant` host mode
monitor (default `homeassistant`); the Probe reports the filesystems mounted into its own
container.

Richer Home Assistant detail (containers, integration health, repairs, backups, Supervisor health,
watchdog breaches and crash classifications) arrives when ha_Int_soc pushes a hostwatch-schema batch
every 60 s to `/internal/v1/ingest` with a `wpi` key created for the host `homeassistant`. The key
cannot push any other host. The contract is in `docs/ARCHITECTURE.md` under "Home Assistant push
contract", and the metric names are unverified against a live push.

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
way to be, so there is no such thing as "Observe running as a gMSA".

1. On a DC, create the service account and its keytab, e.g.:
   ```powershell
   ktpass -princ observe/monitor@LAB.EXAMPLE.COM -mapuser LAB\svc-observe `
     -crypto AES256-SHA1 -ptype KRB5_NT_PRINCIPAL -out observe.keytab
   ```
   (`msktutil` is the equivalent tool if you manage the account from Linux.)
   Use AES256, not RC4, because modern DCs support it and RC4 is what's actually
   being deprecated alongside NTLM.
2. Put `observe.keytab` under `./secrets` (never in `./config`, which the
   container mounts read-only for config, not secrets, and which you may put
   in git).
3. Write a `krb5.conf` for your realm (`[libdefaults] default_realm =
   LAB.EXAMPLE.COM`, `[realms]` with your KDC, `[domain_realm]` mapping your
   DNS domain to the realm) and mount it read-only at `/etc/krb5.conf` (see
   `docker-compose.yml`).
4. In the credential, set `principal` to the keytab's principal and
   `keytab_path` to where the keytab lands inside the container
   (`/run/secrets/observe.keytab`), and reference it from a monitor as
   usual.
5. `kinit -kt` runs automatically before each poll (a ticket is cached for a
   few hours, not re-acquired every time); nothing needs to be pre-authenticated
   on the host. Time skew between the container and the KDC must stay under
   about 5 minutes or Kerberos rejects the ticket outright, so make sure the
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
temperature to a Phyn flow reading, becomes an Observe monitor);
`unavailable` counts unavailable entities with `domains` and fnmatch
`ignore` filters for the ones you know are dead; `updates` counts `update.*`
entities that are on. HA serves plain HTTP unless configured otherwise: set
`https: false` for that, and read THREAT-MODEL.md on what it means.

**UniFi Network and Protect.** Both use the console's Integration APIs with
an `X-API-KEY` header from Settings > Control Plane > Integrations; one key
serves both. Network list endpoints are paged, and every page is read, up to 50 pages of
200 rows and 4 MB per reply; a longer list or bigger reply is refused, and redirects are not followed.
`devices` fails when any adopted device is not ONLINE (use `ignore` for
stale records); `device_cpu` and `device_memory` read
`/statistics/latest`. `ports` reads `GET /devices/{id}` (body capped at 1 MB, redirects
not followed) and reports `detail.ports` keyed by port index with `speed_mbps`,
`max_speed_mbps`, `state` and `poe`; VLAN and PoE watts are not in this API and stay
unknown. The port list shape is unverified against a live console. For a standalone (non-UniFi OS) controller set
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

## Setting up UniFi and Home Assistant sources

Each source below uses the least privilege that works. Put every secret in the environment and
reference it as `${NAME}`; none of them is written to the database or a log.

| Source | What to create | Where it goes |
|---|---|---|
| UniFi Integration API key (view only) | In the console, Settings > Control Plane > Integrations, create one key. The Integration API only reads. | A `unifi` credential, used by the `unifi_network` and `unifi_protect` checks and by the `unifi` plugin. |
| Dedicated local UniFi account (optional) | On the UniFi OS console, add a local-only user with view-only access to Network, with no cloud login, no admin role and no other use. | A `unifi_classic` credential named in `plugin_settings.unifi.classic_credential`. It gives PoE watts, per-port VLAN, LLDP neighbours, uplink port numbers, WAN health and offline clients. The plugin works without it. |
| Home Assistant token (non-admin) | A dedicated HA user that is not an administrator, with a long-lived token from its profile. | A `homeassistant` credential for the `homeassistant` check. Observe reads `/api/config` and `/api/states` only and never holds an admin token. |
| HA SOC Probe SNMPv3 credential | An SNMPv3 authPriv user on the HA SOC Probe add-on, scoped to the HOST-RESOURCES subtrees (see ha_Int_soc `docs/SNMPV3.md`). | An `snmp` v3 credential for the `snmp` monitors in modes `cpu`, `memory` and `storage`, with `host_name: homeassistant`. |
| HA SOC push key | A `wpi` key for the host `homeassistant`, created from the Add host wizard or the key admin page. | Configured in ha_Int_soc only, never in Observe's YAML. It can push only the host `homeassistant`. |

If the view-only key or the local account is revoked, the plugin pauses and backs off instead of
retrying every interval. If you use a UniFi account with more rights than view-only, Observe
cannot detect that, so a stolen password would reach further. Keep the push key out of any
shared dashboard or log, and regenerate it if HA SOC is rebuilt.

## Discovery

```sh
docker compose run --rm observe --config /config/observe.yaml --discover \
  --target 192.0.2.0/24 --target 198.51.100.10-40 --target dc01.lab.example \
  --credential snmp-v3-core --credential win-monitor \
  --out /data/proposals.yaml --report /data/discovery.json
```

Targets can be CIDRs, ranges (`192.0.2.10-40` or `192.0.2.10-192.0.2.40`),
single IPs, or hostnames, on the command line or under `discovery.targets`.
Credentials are names from `credentials:` and are tried in order; nothing
else is ever sent. Discovery writes a proposal file and changes nothing: you
copy what you want into `observe.yaml`, then `--validate`, `--once`, restart.

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
`observe_group_state`.

## Capacity forecasting

Set `forecast: true` on any monitor that has numeric values and thresholds
(disk, memory, interface utilization, UPS charge). Once an hour, and on
demand at `/api/forecasts?refresh=true`, Observe averages the last
`lookback_days` of values into hourly buckets, fits a least-squares line, and
projects when it crosses the warn and crit thresholds. Results appear on
each monitor card, in a "Capacity outlook" list sorted by soonest crossing,
and in `/metrics` as `observe_forecast_seconds{level="warn|crit"}`.

Every projection carries its r-squared. Below `min_r2` it is labelled low
confidence rather than hidden. Flat or receding trends, too little history,
and crossings beyond `horizon_days` return a status and reason, never a
date. A straight line ignores daily and weekly cycles; treat projections for
seasonal series with that in mind. Forecasts inform; they do not alert.

The methods and their public sources are recorded in `docs/PRIOR-ART.md`.

## Alerts

`ntfy`, `webhook` (JSON POST), `smtp`, and `mqtt`. The MQTT target publishes a
retained `observe/<slug>/state` (`up`, `warn`, `down`) and a non-retained
JSON `observe/<slug>/event`, which Home Assistant can consume with an MQTT
binary sensor. Each target has `notify_on` (default `[down, up]`) and each
monitor can restrict itself to named targets with `alerts:`. Failed deliveries
are retried once, then shown in the dashboard footer.

## Adding a host

Admins add a host in the console at `/hosts/new` (Hosts, Add host in the navigation), a
five-step wizard: host name and platform, agent and control, the control allowlist, the
one-time install command with a Copy button, and live progress. The wizard uses this API.
A fan header is 1 to 32 letters, digits, dashes or underscores and does not start with a dash,
the same rule hostwatch-control applies. Copy needs a secure context (HTTPS or localhost); on
plain HTTP the button selects the command and says "Selected, press Ctrl+C to copy.". Moving
between steps puts focus on the step heading and announces it to screen readers.
`POST /api/hosts`
(admin session and CSRF) takes a host name, a platform (`linux`, `truenas`,
`windows` or `raspberry-pi`), whether to enrol the agent and control, and a
control allowlist. It returns an install command headed with the host name and
platform, backed by a single-use token that lasts 30 minutes and is stored only
as a digest. The host's `wpi` and `wpc` keys are created when the token is
redeemed. `GET /api/hosts/{name}/enrolment` reports progress: script fetched,
first data, control first pull, ready or expired, plus `token_state` (valid, used or
expired) and `guard`, the reason the script last refused to run on a machine. Control is not offered for
Windows yet, and not for TrueNAS. `GET /i/{token}` serves a guarded install script
that holds no key and does not spend the token. The script runs its checks first and only
then calls `POST /api/enrol/redeem`, which spends the token and returns the keys, so a
command run on the wrong machine is refused, the reason is shown in the wizard and on the
host settings page (for example "ran on ai-pi, expected MediaIn-SVR"), and the same
command still works on the right machine. The command's address is never taken from the
request's Host header: set `server.public_url` in the config (`http(s)://host[:port]`, never
a localhost name), or the wizard asks an admin to confirm the address once and saves it.
The script for every platform: a shell script for `linux` and `raspberry-pi` hosts (S11b) that
installs the agent container and, if chosen, the control daemon, a shell script for
TrueNAS SCALE that writes `agent.env` and the compose file on the chosen pool (default
`Apps`) and prints the one step you do in the TrueNAS web UI, and a PowerShell script
for Windows that runs hostwatch's own installer with the key as a secure string (S11c).
Each refuses to run on the wrong machine or on the Observe host before it changes
anything, and reports each step back. The command block is headed with the host name and
platform, so it cannot be mistaken for another machine's. If the token expires before
the script is redeemed, the wizard shows "Command expired" with a Regenerate command
button, which calls `POST /api/hosts/{name}/enrolment/regenerate` (admin session and CSRF)
and replaces the token, so the old command stops working. Once the token has been redeemed
the wizard and the host settings page show "Command already used" with the same button,
which then calls reissue (it asks first, because it revokes the keys the old command made).
A host that has been enrolled but has not reported yet is listed under `waiting` in
`GET /api/hosts` and in a "Waiting for first data" card on the dashboard, with a link to its
enrolment page. See `docs/ARCHITECTURE.md`, "Host enrolment".

## Host settings

Admins open a host's settings from the Settings button on its host page, at
`/hosts/{name}/settings`. The page shows the host's identity, the control allowlist, the install
command and a danger zone.

- **Allowlist.** Edit the fan headers (with the lowest remote duty per header), the restartable
  services and the reboot choice. Save shows a confirm dialog that lists the changes, then
  `PUT /api/hosts/{name}/allowlist` stores the new list. When the install command was already run
  the response also holds a short update command, headed with the host name, that rewrites
  `control.toml` and the sudoers rules on that host and restarts the control service. It has the
  same guards as the install script: root, the host name, and not the Observe host. The status
  chip reads Pending until the update has run, Written once `control.toml` holds the list, and
  Applied after the host's control service next pulls with its key.
- **Install command.** Regenerate command revokes the old token, the agent key and the control key
  of the host after a confirm dialog, and shows a new headed command with live progress. The
  saved allowlist goes into it. The host's stored data is kept.
- **Clean up this machine.** Makes a command for undoing an install that went onto the wrong
  machine. It removes only an install made for this host, and refuses any other machine before it
  changes anything.
- **Danger zone.** Revoke host keys and Remove host both need the host name typed.

The update and cleanup commands are served by `GET /t/{token}`, with a single-use token that
lasts 30 minutes, like the install command. See `docs/ARCHITECTURE.md`, "Host settings".

## Direction: no longer read-only

The owner reversed the earlier decision that Observe is read-only by
design. It is becoming the single monitoring UI and, in a later phase, the
control plane, replacing the hostwatch hub and web view. The first phase adds
ingest from hostwatch agents, logins, an admin role, and an audit log, and
builds no action that changes a host. The design and its limits are in
`docs/ARCHITECTURE.md`, and the new risks are in `THREAT-MODEL.md`. Parts of
this README describe the read-only behavior of the current release; they are
updated as each phase lands.

The SQLite store is now versioned. On startup Observe applies any missing
additive migrations, keeps all existing history, and refuses to open a
database written by a newer version. `server.retention_days` governs poll
results and host samples (minimum 1), and the new `server.audit_retention_days` (default
365, minimum 1) governs the audit log independently. The tables for hosts, keys,
users, sessions and audit are filled by the ingest and login routes.

The hostwatch wire schema models exist in `observe/ingest/schema.py`, with
size and count limits, ignoring of unknown fields, and rejection of unknown schema
versions. `POST /internal/v1/ingest` (the path hostwatch agents use; `/api/ingest`
is an alias) in `observe/ingest/api.py` receives batches. A batch without
`batch_id` is deduplicated by a content hash.
It takes `Authorization: Bearer <ingest key>`, rejects a body over 1 MiB, and
stores samples, source status and events only when the key is valid and bound
to the host named in the body. The answers are 401 for a missing, wrong or
revoked key, 403 for a valid key bound to another host, 413 for an oversized
body, 400 for a body nested deeper than 32 levels (checked before parsing, and
recorded as a denial), 422 for a body that fails the schema, and 429 over the per-peer rate
limit (`server.ingest_rate_per_minute`, default 120). A batch that repeats a
`batch_id` is acknowledged with `"duplicate": true` and stored once, so an
agent can replay its outbox safely. Denials are written to the audit table at
most once per peer per minute, with a count of the denials the row covers. The
endpoint does not use the optional dashboard basic auth, because agents carry
their own key.

Boot events from the agent are classified as clean, crash or unknown. The
classification is stored in the event detail, and the host row records the
current boot id and whether the previous boot ended cleanly. An unrecognised
boot kind is unknown, never clean.

Event severity is normalized without regard to case to info, warning or
critical. `fatal`, `error` and `emerg` count as critical, and an unknown value
counts as warning, with the original kept as `severity_raw`. A crash boot (panic,
watchdog reset, power loss, unknown unclean) turns the `pushed_host` monitor to
WARN, or DOWN when `crash_result: fail`, for `crash_hold_s` seconds (default
3600), and alerts through the normal confirmation. A clean reboot does not.

A pushed host becomes a monitor when you list it in the YAML with
`type: pushed_host` and the `host` name its agent sends. Listing it is the
confirmation: a host that pushes but is not listed is stored and never alerts.
Nothing is polled. Each check reads the latest batch and grades every entry in
`components` (a `source` and `metric` with `warn` and `crit` thresholds, same
`direction` rule as other monitors) as Good, Warning or Critical. Critical maps
to a failed check, Warning to a warning, and Good to OK, so the usual
`failures_to_down` and `recoveries_to_up` confirmation applies before anything
pages. `require_sources` names sources that must be available; one that is not
is a Warning. A null reading is skipped, never treated as zero. If no batch
arrives within `stale_after` seconds (default three intervals), or none ever
arrived, the check fails like an unreachable host. A component whose newest reading is older than `stale_after` is graded stale and also fails, even when a recent batch arrived. Timestamps more than 300 seconds ahead of receive time are clamped to receive time, and a replayed batch older than the stored one (by `sent_at`) does not overwrite the host row or source status. `group`, `depends_on` and
`critical` work as for any monitor, so a host appears in its group's rollup,
is suppressed when a parent switch is down, and raises alerts. `/metrics` gains
`observe_host_age_seconds` and `observe_host_component_state` (0 good, 1
warning, 2 critical) for pushed hosts, alongside the usual state, effective
state and group lines.

Each pushed host also has a hardware page at `/host?name=HOST`, linked from its
row on the dashboard (a card in its group, with a status chip). `GET /api/hosts` lists every host that has pushed (and
every listed `pushed_host` monitor that never has), and `GET /api/hosts/HOST` (a host name may contain slashes; the route takes the rest of the path)
returns its CPU, memory, power, temperatures, fans with the fan controller
state, RAID, ZFS pools, disks, UPS, recent alerts and events, boot state and
sources. Every section and every reading is Good, Warning or Critical. The
built-in limits are fixed in `observe/hostview.py`, and thresholds listed on
the monitor in the YAML override them for that source and metric. The page says
so when data is missing: a reading with no value is a Warning and is never
shown as zero, a reading or source older than the stale window is marked stale,
a source that failed shows its reason, a source the agent says the host does not
have is shown as not present without making the host look worse, and a host that
has stopped pushing is Critical. These routes need a login session; basic auth
does not open them. They only read; no action that changes a host exists.

Hosts set up through `/hosts/new` get their keys automatically. Ingest keys are also managed on the
admin screen (below) or from the command line, which is the manual route for a host that cannot use
the wizard.
`python -m observe --config observe.yaml --ingest-key-create HOST` prints a
new key once and stores only a hash; the key works for ingest and only for
that host name. `--ingest-key-list` shows each key's id, host, state and last
use, and `--ingest-key-revoke ID` revokes one. The keys are accepted only
by `POST /api/ingest`.

A key has a scope, shown by its marker. `wpi` is host ingest. A listed plugin
may register another scope; the Pockethernet plugin registers `wpf`, for field
report uploads. Create one with `--ingest-key-scope wpf` (HOST is then the
device label, for example the phone's name) or with the `scope` field on the
admin screen's create call. Only `wpi` and the scopes of listed plugins can be
issued. A `wpf` key is refused by host ingest and a `wpi` key is refused for
field reports, whatever the label says. The `pockethernet` plugin lives in
`plugins/pockethernet` and is installed into the image with
`pip install ./plugins/pockethernet`; it needs `plugins: [pockethernet]` to load.
The upload endpoint arrives in a later slice.

The `control` plugin (`plugins/control`, `pip install ./plugins/control`, listed as
`plugins: [control]`) registers the `wpc` scope: a control key bound to one host name, for the
hostwatch-control daemon to pull commands. A `wpc` key is refused by host ingest and field
reports, and `wpi` and `wpf` keys are refused by the control routes. The plugin signs commands
with an Ed25519 key. Create one with `python -m observe --control-keygen /run/secrets/observe_control_key`,
which writes the private key with mode 0600 and prints only the public key to pin on each host;
the plugin refuses to start if that file is readable by group or others on POSIX. Set the path
with `plugin_settings.control.signing_key_file`. The plugin also keeps the signed command
queue: `GET /api/v1/control/commands` returns a host's own unexpired, unfinished commands and
`POST /api/v1/control/results` records the outcome, with output redacted and truncated. A command
that expires without a result is shown as `unknown`, and each host is limited to one pending
command per action, 10 commands per hour and one reboot per 15 minutes. An admin requests an action from the
Control section of the host page (`fan.set_floor`, `fan.set_mode`, `service.restart` or `host.reboot`): the page
validates the parameters against what the host last reported, asks for confirmation in a dialog, and for a reboot
requires the host name typed exactly. The same section lists the host's command history with each state and a
Cancel button while a reboot is scheduled. The section is the last card of the host page, shows states as status chips, and reports a refusal as an inline notice and a toast. The admin routes need an admin session and the CSRF token. The signing format and a test vector are in `docs/CONTROL.md`. Setup in short: install the plugin and run `--control-keygen` once, then add each host with the wizard at `/hosts/new`, which pins the public key in that host's `control.toml` and mints its `wpc` key. The manual steps remain in `docs/CONTROL.md` for a host that cannot use the wizard. The endpoints, setup steps and threat notes are in `docs/CONTROL.md` and `THREAT-MODEL.md`.

Logins use Argon2id password hashes and server-side sessions. Create the first
admin with `python -m observe --config observe.yaml --create-admin NAME`; it
reads the password (12 characters or more) from `OBSERVE_ADMIN_PASSWORD` or a
prompt, never from an argument. Sign in at `/login`. The session cookie is
HttpOnly, Secure and SameSite=Strict, and expires after 30 idle minutes or 12
hours in all (`server.session_idle_s`, `session_absolute_s`). Set
`server.session_cookie_secure: false` only when serving plain HTTP on a trusted
network. An account locks for 15 minutes after 5 failures, and logins are
limited per peer address. Routes that change state need the session's CSRF
token in an `X-CSRF-Token` header (`GET /api/session` returns it), and admin
routes (everything under `/api/admin/` and `GET /api/audit`) also need an admin
user. The optional basic auth is kept for the read-only API and `/metrics` and
is never accepted for admin routes. A session also opens the read-only API. No
action that changes a host exists yet.

The admin screen is at `/admin`. It lists ingest keys and users, creates a key
bound to one host name, revokes a key, creates a user, disables or enables a
user, grants or removes the admin role, and links to the audit log. Revoking a key
and disabling a user ask for confirmation in a dialog. A new
key is shown once, in the response to the create request, and cannot be shown
again, in a read-only field with a Copy button; only its hash is stored. Disabling a user ends their sessions at once.
The last active admin cannot be disabled or demoted. Every change is a
request carrying the CSRF token and is written to the audit log. The page
itself is a static file with no data and sends a visitor without a session to
the login page. It has no control for any host.

The audit log (`observe/audit.py`) records logins, failed logins, logouts,
user creation, key creation and revocation, and rejected ingest. When an action
fails partway, such as a refused user, a login that cannot create a session, a
failed key action or a batch the store could not write, a separate `*_failed`
or `*_error` row says so. Passwords, tokens and keys are never written, paths
are sanitized, and `GET /api/audit` (admin session only; parameters `limit`,
`kind` and `before`) returns the rows newest first. The audit page at `/audit` (Admin menu,
admin only) shows the newest 500 rows in a sortable table with filters for actor, kind, status
(OK, Refused, Failed) and time range, and pages of 25 or 100.

## Plugins

Observe can load plugins, which are Python packages installed into the image
that publish an entry point in the group `observe.plugins`. Installing one
does nothing until you list it in the config. For compatibility, a plugin still
published under the legacy group `watchpost.plugins` (the old name) also loads, and
one warning names it. Plugin names in the config are unchanged:

```yaml
plugins: [pockethernet]
plugin_settings:
  pockethernet: {}   # validated by the plugin's own settings model
```

A listed plugin that is not installed, or that does not support this Observe
version, stops startup with a message naming it (`--validate` checks this too).
Plugin routes live under `/api/plugins/<name>/` and need a login
session, the CSRF token for anything but a read, and count against
`server.plugin_rate_per_minute`; state changes and refusals are written to the
audit log. A plugin may also declare a route that takes a key of its own scope
instead of a session, which the core checks and audits in the same way. A plugin keeps its own tables and schema version in the same database;
its migrations run at startup, and a database written by a newer release of the
plugin is refused. Removing a plugin from `plugins:` hides its pages, navigation
entries and routes but keeps its data, so listing it again resumes where it
stopped. A plugin's navigation entry may name the console `workspace` it belongs under
(`overview`, `hosts`, `network`, `reports` or `admin`; the default is `network`). Plugin pages live under `/plugins/<name>/` and need a login. A plugin may also declare periodic collectors: async jobs with an interval of at least 30 seconds and a timeout no longer than the interval. The scheduler runs each in its own task, once at startup and then on its interval. A failure or timeout is logged once per streak and never stops other collectors or the scheduler, and a plugin with a shorter interval is refused at startup. Plugins run in the same process with full trust, so install only
plugins you trust. The design is in `docs/FIELD-DATA.md`.

The Pockethernet plugin accepts field reports from the phone at
`POST /api/v1/field-reports` with a `wpf` key (`Authorization: Bearer wpf_...`), and
`GET /api/v1/field-reports/ping` checks a key and returns the server time. A body is
JSON, at most 256 KiB, and may be gzip with `Content-Encoding: gzip` if it inflates to
no more than 256 KiB at no more than 50 times its compressed size. A report is kept by
its `report_id` and `revision` per phone: a higher revision replaces it, the same one
is a duplicate and a lower one is ignored. A phone whose clock is more than 5 minutes
off is corrected, using the optional `X-Report-Sent-Ms` header, and the report is
flagged `clock_corrected`. Each upload counts against `server.plugin_rate_per_minute`
and is audited (valid keys are counted per key and per peer, failed keys per peer separately). The raw report is evidence and is kept for
`plugin_settings.pockethernet.evidence_retention_days` (default 365) before the body
is dropped. Each accepted report is also turned into map data: the LLDP or CDP neighbour
gives a switch and port, the site port id gives a jack patched to that port, and the
report's measured properties are added to the port's history with the report, key and
tester recorded. A jack that turns up on another port closes its old link. An admin can
clear and recreate all of it from the stored reports with
`POST /api/plugins/pockethernet/rebuild` (admin session and CSRF token, audited); it is
refused if retention has already dropped any report body.

```yaml
plugin_settings:
  pockethernet:
    evidence_retention_days: 365
```

The core also keeps an infrastructure map in the same database: switches, ports, wall jacks,
links, endpoints and port properties with history. Port names are normalised, so
`GigabitEthernet1/0/5` and `Gi1/0/5` are one port while `Gi1/0/5` and `Gi1/0/50` stay
apart. Plugins write to it through `observe.infra.InfraService`. A switch is matched to a
monitor by chassis MAC, management address or sysName, and a port to an SNMP interface monitor
or a UniFi device port. Matching never creates a monitor, and an ambiguous match is left for
an admin. Switches that match nothing are listed at `GET /api/admin/infra/unlinked` and an
admin links one with `POST /api/admin/infra/link` (audited). `GET /api/infra/findings` lists
conflicts between field results and live state: speed above live, VLAN mismatch, PoE verified
but no power, and re-patched. It also lists field changes between the last two reports of a
port: speed drop, new cable fault, length change, PoE drop, VLAN change, DHCP fail and a worse
cable verdict. Findings are computed on request, are shown on the dashboard, port page and map
only, and never send alerts.

`GET /api/infra/map` returns nodes and edges with live state from the matched monitors and
can be filtered by `site` and `building`. A link nobody has confirmed for `map.stale_days`
(default 90) is stale, after twice that it is hidden, and a report that moves a jack or an
uplink closes the old link at once. Dependencies are inferred from the links: when
`map.auto_depends` is true (the default), an edge confirmed by LLDP or CDP within
`stale_days` is applied and feeds the rollup, so everything behind a DOWN switch shows
UNREACHABLE and only the switch alerts. Weaker edges are listed at `GET /api/infra/dependencies`
until an admin accepts or rejects them (`POST /api/admin/infra/depends/accept` and `/reject`,
audited). An edge that would create a cycle is refused and listed.

Three pages show this data and need a login. `/map` draws core, distribution, access, jacks
and endpoints from `GET /api/infra/map`, spells out each node's state in words as well as
colour, and filters by site and building. `/port?switch_id=...&port=...` shows one port: live
state from its matched monitors, current properties, property history, findings and the
matched monitors. An admin can acknowledge a finding there (`POST /api/admin/infra/findings/ack`,
audited); an acknowledgement covers that finding's current message only, so a changed finding
shows as new again, and it never sends an alert. `/admin/infra` lists the unlinked switch queue
with a monitor choice for each, and the pending dependency proposals with Accept and Reject
(each asks for confirmation, then a toast reports the result and the row moves to Decided
without a page reload). The admin, audit, map admin and port pages use the shared cards,
sortable tables, status chips, dialogs and toasts.

The Pockethernet plugin adds pages that need a login: `/plugins/pockethernet` lists field
reports, `/plugins/pockethernet/report?source=...&report_id=...` shows one report with its
typed sections first and then the raw steps and tool results, and
`/plugins/pockethernet/jack?key=...` shows a jack, the ports it has been patched to over time and
its reports. These pages use the shared console shell, sortable tables and result chips (Pass, Fail,
Warn, each an icon and a word). The report and jack pages draw the wiremap as SVG with each pair
labelled by number and colour name, and the list has a filter box. The dashboard links to the list as "Field reports" and shows a "Field findings"
list; a warning that nobody has acknowledged turns a port that passes its live check to
Warning on the port page and the map.

### The UniFi plugin

The `unifi` plugin lives in `plugins/unifi` and is installed into the image with
`pip install ./plugins/unifi`; it needs `plugins: [unifi]` to load. This release holds the
settings, the tables, the devices, clients and Protect collectors and the UniFi page. Every
`interval` seconds (default 120) it reads the site list and the device list from the UniFi Network
Integration API with the API key of a `unifi` credential, and keeps the current snapshot in
`unifi_devices`, `unifi_clients` and `unifi_cameras`, with a first seen and last seen time and no
per-poll history. A record not seen for `retention_days` (default 30) is deleted.
The same poll feeds the infrastructure map: each device becomes a switch keyed by its chassis
MAC, so a `unifi_network` monitor whose `device` is that MAC matches it automatically, and a
device that names its uplink device gets a `config` link to it.

```yaml
credentials:
  unifi_key: {type: unifi, api_key: ${UNIFI_API_KEY}}
  unifi_view: {type: unifi_classic, username: observe, password: ${UNIFI_VIEW_PASSWORD}}  # optional
plugins: [unifi]
plugin_settings:
  unifi:
    host: 192.0.2.1
    credential: unifi_key
    site: Default          # the only site when omitted
    interval: 120
    retention_days: 30
    clients_interval: 300  # seconds between client polls
    protect: false         # true polls Protect cameras with the same key
    protect_interval: 120
    # classic_credential: unifi_view   # optional; enables the read-only classic client
```

The settings of `plugin_settings.unifi` are:

| Key | Default | Meaning |
|---|---|---|
| `host` | required | Console address. |
| `port` | 443 | Console port. |
| `https` | true | Use TLS. |
| `verify_tls` | true | Validate the console certificate. |
| `ca_bundle` | none | Path of an internal root certificate. |
| `base_path` | `/proxy/network/integration/v1` | Network Integration API root. Use `/integration/v1` for a standalone controller. |
| `credential` | required | Name of a `unifi` credential. |
| `classic_credential` | none | Name of a `unifi_classic` credential; enables the classic collector. |
| `site` | the only site | Site name. |
| `interval` | 120 | Seconds between device polls, 30 to 3600. |
| `clients_interval` | 300 | Seconds between client polls. |
| `protect` | false | Poll Protect cameras. |
| `protect_base_path` | `/proxy/protect/integration/v1` | Protect Integration API root. |
| `protect_interval` | 120 | Seconds between camera polls. |
| `timeout` | 20 | Seconds for one poll, at most 120. |
| `retention_days` | 30 | Delete records unseen this long. |

Clients are read every `clients_interval` seconds (default 300) and kept one row per MAC. With
the classic account each connected client also gets its access point or switch, switch port number
and SSID, and clients the console knows but that are not connected are added as offline with the
time the console last saw them (older than `retention_days` they are left out). A client the
console stops listing is marked not connected, not deleted. With `protect: true` a camera
collector reads the Protect camera list every `protect_interval` seconds (default 120): state,
connected and, only when the console gives a boolean `isRecording`, recording. NVR storage is not
read because no route for it is verified.

The **UniFi** page under Network (`/plugins/unifi`, signed in users only) has Devices, Clients and
Protect tabs. The Clients tab filters by text, kind and state and draws only the rows in view, so
a few thousand clients stay fast. The page shows what the last poll stored, not live data. The
Devices tab shows when it was last updated and a stale marker when the devices collector has not
succeeded within twice its interval.
The collector sends only GET requests, never follows a redirect, refuses a response over 4 MB
and a list over 50 pages. After a 401 or 403 it stops sending requests, waits one interval, and
doubles the wait on each further rejection up to one hour, so a revoked key is not hammered.
The optional classic account is a dedicated local view-only account. With it the plugin can log in
and read PoE watts, per-port VLAN, LLDP neighbours, uplink port numbers, WAN health and offline
clients. It sends only the login and logout POSTs and GETs of four read views, keeps the session in
memory, re-logs in once on a 401 and then backs off, and never follows a redirect. A login answered
429, a 5xx or any other failure also backs off, doubling up to 30 minutes, and the pause resets only
when a read succeeds. A 403 on a read after a good login is logged once as a permission problem and
backs off the same way instead of retrying every poll. If a classic read fails, a client keeps the
SSID, switch port and uplink last read, with the time of that read, instead of losing them. A port
that is down reports no link speed. An offline client the console gives no last seen time for is
not refreshed on every poll and ages out with the retention. The values are
used by a second collector, `classic`, that runs only when `classic_credential` is set. It adds
each port of each device to the map (`Port N`, with the UniFi port index), writes link speed, PoE
class, PoE watts and VLAN as port properties with the source `unifi` only when a value changes,
and adds `config` links from the uplink port numbers and `lldp` links to neighbours that are
already known switches. With that data a Pockethernet VLAN, speed or PoE result on the same port
is compared with what the switch reports, and without it nothing is compared. The classic field
names are unverified against a live console.

## Not implemented

Network discovery, automated remediation actions, native DCOM WMI,
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

Set `OBSERVE_REQUIRE_SERVICES=1` to make missing test services a failure
rather than a skip; CI does this.

`tests/test_ui_static.py` is a static guard. It fails if any page or script under
`observe/static` or a plugin adds inline script or style, `style=` or `on*=`
attributes, `innerHTML` and its relatives, `eval`, or an off-origin URL, or if a
page route loses its Content-Security-Policy header.

`tests/test_ui_tokens.py` checks the colour tokens in `observe/static/css/tokens.css`: every
token is present in the light block and both dark blocks, the two dark blocks match, and the
text, status and focus colours meet WCAG contrast (4.5:1 for text, 3:1 for dots, the focus
ring and the border of text fields) in light and dark. The console theme (Auto, Light or Dark) is a per-browser choice kept in
`localStorage` under `observe.theme`; the server never stores it.

`observe/static/css/components.css` and the modules `js/chips.js`, `js/table.js`, `js/dialog.js`
and `js/toast.js` are the shared building blocks for status chips (icon plus word), cards, KPI
tiles, sortable tables, confirm dialogs and toasts. `tests/test_ui_components.py` checks them,
and `node --test tests/js` runs the pure sort and chip tests where Node is available (CI only).

The force-directed graph engine for the infrastructure map is in `observe/static/js/graph/`
(`force.js`, `render.js`, `view.js`) with `css/graph.css`. It has no dependencies, runs the layout
once (at most 360 steps, and only up to 300 nodes), paints to a canvas in the theme's colours and
shows each node's state as a ring and a glyph, not by colour alone. It is ported from
relationship-maps via ha_Int_soc (both MIT) and is used by the Network map page (graph, tiers and table views, chosen with `#graph`, `#tiers` or `#table`; tiers is the default on phones and above 300 devices). `tests/test_ui_graph.py`
checks it, and `tests/js/graph.test.mjs` holds the layout tests for CI.
