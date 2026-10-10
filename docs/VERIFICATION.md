# Verification status

What has been exercised against real software, what has only been tested
against stubs, and what has not been run at all. Update this file when a
row changes.

As of 2026-09-23, 125 tests pass on Python 3.12 (Ubuntu 24.04, net-snmp
5.9.4, Mosquitto from the Ubuntu archive).

The owner reversed the earlier decision that Observe is read-only by
design (see `docs/ARCHITECTURE.md`). Ingest, logins, the admin role, the
audit log, and the control channel are designed but have no verification
yet; rows for them are added here as they are built and exercised.

## Verified against real services

| Area | How |
|---|---|
| SNMP v2c and v3 authPriv (SHA/AES) | Loopback `snmpd`: uptime, string and numeric OIDs, missing OID, wrong community (with redaction), interface by name with rate on second poll, memory via `hrStorageRam`. |
| MQTT check and MQTT alert | Loopback Mosquitto: connect, retained numeric payload with thresholds, expect mismatch, silent topic timeout, broker down, retained state publish. |
| TCP, HTTP, TLS certificate | Local asyncio, http.server, and TLS servers with generated certificates (valid, near expiry, expired-threshold, untrusted chain, verify off). Over repeated polls: one client TLS context per setting while the server still sees a new connection and a full handshake each poll, a replaced, missing or invalid `ca_bundle`, redirects as the monitor sets them, and no ALPN carried from an HTTP poll into a certificate probe. |
| ICMP | Loopback with unprivileged ICMP enabled. |
| State machine, thresholds, config validation, secret references | Unit tests. |
| Web API, basic auth, CSP header, metrics label escaping | FastAPI test client. |
| Dependency suppression and group rollup | Scripted-result tests: suppression, on-demand parent confirmation (including a never-polled parent at startup), transitive root-cause attribution, silent recovery of suppressed children, re-poll and release of children still failing after parent recovery, worst-of and non-critical group state. Cold-start demo run confirmed the suppression event. |
| Forecasting math | Synthetic series: slope recovery, warn/crit dates for rising and falling resources, flat, receding, beyond horizon, already crossed, low r-squared labelling, insufficient history, hourly bucketing that skips failed polls and nulls. |
| Discovery | Target expansion (CIDR, both range forms, exclude, dedupe, size cap before allocation). Loopback end to end with real snmpd and mosquitto: wrong community falls through to the right one, existing monitor skipped, proposals merged into a config that validates. Real HTTPS server: valid by hostname, not valid by bare IP. WinRM TLS gate (no credentials sent to an unvalidated listener) and lockout serialisation (12 parallel hosts, exactly 3 attempts). A /24 of empty space scanned in about 14 s. |
| Linux over SSH | Real OpenSSH server (tests/sshd.sh): cpu, memory, load, disk (including a non-mount-point path), uptime, command, key and password credentials, unknown and changed host keys, injection attempts rejected at load. |
| Docker over SSH | Real SSH to a fake docker CLI (tests/fixtures/fake-docker) that emits the documented output shapes: healthy, unhealthy, exited, missing, restart-count growth, summary with restart policies. |
| SSH discovery | Trust-on-first-use with key only (password withheld), key recorded, TOFU off, changed key, unparseable entry, wrong password retired at the limit under parallel attempts, proposals merge into a valid config. |
| AD enumeration logic | ldap3's in-memory mock server: paged search, disabled, stale, never-logged-on, os_include, FILETIME and datetime timestamps, end-to-end discovery of directory names with non-responders listed. |
| vSphere | vcsim (VMware govmomi simulator v0.56.0, ESX mode), which serves the real vSphere SOAP API: host, host CPU and memory, datastore, VM, missing VM, bad password, certificate pinned via a non-CA self-signed leaf, unpinned certificate rejected, discovery with inferred VM-to-host dependencies. |
| Proxmox | Local HTTPS fake returning /cluster/resources with field names taken from SDKs generated from the official API schema: node modes, guest by name and VMID, stopped guest, storage, 401, token with no visible resources, TLS gate, discovery with guest dependencies and shared storage de-duplicated. |
| TrueNAS | Local WebSocket fake implementing auth.login_ex (API_KEY_PLAIN, as documented), pool.query (documented fields), alert.list, and interleaved notifications: pools, pool usage, alert level filtering and dismissed alerts, bad key, discovery. |
| Home Assistant | HTTPS fake with the documented REST shapes (`/api/` "API running.", `/api/states`, `/api/states/<id>`, Bearer auth): api, bad token, numeric entity with thresholds, expect, unknown, missing entity (404), unavailable with ignore and domain filters, updates, entity_id validation, discovery. |
| UniFi Network and Protect | HTTPS fake with X-API-KEY auth, paged device lists (page size 2, so paging is exercised), device fields and statistics fields as recorded from a real Network 10.6.106 controller, Protect camera states from the published enum: summaries, ignore, device by name and MAC, CPU and memory, offline device, firmware, bad key, unknown site, cameras, info, discovery. Discovery originally read only the first page of devices; the paging test caught it. |
| Technitium | HTTP fake returning the documented stats fields and status envelope: SERVFAIL rate, Bearer header used and no token in the URL, legacy query-token mode, invalid-token status, update check, discovery refusing plain HTTP and proceeding when allowed. |
| Dashboard rendering | Screenshot at 390 px, dark scheme, with DOWN, UNREACHABLE, non-critical, group pills, and a capacity outlook row. |
| Whole service | End-to-end run: scheduler polled six monitors, stored history, emitted a DOWN transition and MQTT alert, served dashboard and API with auth. |
| Dashboard JavaScript | Syntax-checked with `node --check`. |

## Tested only against stubs

| Area | Gap | How to verify |
|---|---|---|
| WinRM service/cpu/memory/disk/powershell | pywinrm `Session` was replaced by a fake; script text and parsing are tested, the wire protocol is not. | `--once --only <slug>` against one Windows host with each mode. |
| WMI over WinRM | Same as above. `ConvertTo-Json` output shapes (single value vs array) are handled, but not observed from a real host. | Run a `count` and a numeric query against a real host. |
| WinRM certificate transport | Only that PEM paths reach pywinrm. | Map a client cert on a test host and run a service check. |

## Not yet run

| Area | Note |
|---|---|
| Docker image build (amd64, arm64) and the compose hardening settings | CI builds both architectures; the first CI run is the first build. |
| GitHub Actions workflow | Action versions (`checkout@v4`, `setup-python@v5`, `setup-qemu-action@v3`, `setup-buildx-action@v3`, `build-push-action@v6`) were written without looking up current releases. |
| DNS success path | Only the unreachable-nameserver failure is tested. |
| SNMP `cpu` mode success path | The test agent does not populate `hrProcessorLoad`; the "not exposed" path is tested. |
| ntfy and SMTP delivery | Code paths exist; only webhook failure reporting is tested. |
| Forecasts on real long-running data | Only synthetic series so far. Seasonal devices (daily backup growth, office-hours CPU) will produce low r-squared until Holt-Winters exists. |
| Discovery against real network gear and Windows | The WinRM probe script and role-service detection ran only against a stub. SNMP interface selection was only tested against Linux net-snmp; switches with unusual ifType values may need LINK_IF_TYPES extended. |
| LDAPS against a real domain controller | Only the mock. Real binds, certificate validation, paging past 1,000 results, and the default read permissions are untested. |
| Real Docker Engine | Only the fake CLI. The `--format` templates follow Docker's documented template fields but have not been run against a real engine. |
| systemd service mode success path | The test container has no systemd; only the failure path ran. |
| Linux on distributions other than Ubuntu | BusyBox `df` (Alpine) lacks `-x`; discovery falls back to `/` only there. |
| Real TrueNAS, Proxmox, ESXi | None have been polled for real. TrueNAS `alert.list` field names (level, dismissed, formatted, klass) and the level names were written from knowledge of the API, not looked up. vcsim reports no guest heartbeat or Tools status, so the red/yellow heartbeat paths ran only as code, not against a simulator value. |
| Real Home Assistant, UniFi, Technitium | Not polled for real. UniFi Network pagination parameters (`offset`, `limit`) and the `totalCount` envelope field, the Protect `/meta/info` field name, and Technitium's `invalid-token` status string and `checkForUpdate` field names are from third-party clients and memory rather than the vendors' reference pages. |
| Citations in docs/PRIOR-ART.md | Written from general knowledge; add URLs after checking each primary source. |
