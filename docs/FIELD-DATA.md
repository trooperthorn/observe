# Field data from the Pockethernet app

Status: design; the plugin loader, per-plugin migrations, plugin pages, the infrastructure tables with port keys and property history, monitor matching with conflict findings, and map data with ageing and inferred dependencies are built, the rest is not. Owner decisions recorded 2026-10-04 are in the last section. This document describes how test results from the
Pockethernet Android app (repo `pocketethernet-app`) become properties of switch
ports in watchpost, and how the mapping data in those results builds an
infrastructure map with live availability and health.

## Goals

1. Field-verified facts about a port become custom properties on that port:
   wall jack and patch panel label, room and site, cable result and length per
   pair, verified link speed and duplex, PoE class and power, VLAN and voice
   VLAN, DHCP and DNS result, and when and with which tester it was last tested.
2. LLDP and CDP neighbour data, the app's site model (site, building, room,
   panel, port) and its port map build a topology of switches, ports, jacks,
   endpoints and uplinks. watchpost overlays live state from its existing
   monitors and infers dependencies from that topology.

## Data model

All tables are added by one additive migration. Nothing existing changes
except a new `scope` column on `ingest_keys`.

### Infrastructure entities

| Table | Key | Holds |
|---|---|---|
| `infra_switches` | `switch_id` | Normalised identity: LLDP chassis id (MAC) when known, else lower-cased sysName. Display name, management addresses, vendor, platform, `matched_monitor` (SNMP, UniFi or ping monitor), first and last seen. |
| `infra_ports` | `(switch_id, port_key)` | `port_key` is the normalised port name, so `GigabitEthernet1/0/5`, `Gi1/0/5` and an SNMP ifName of the same port compare equal. Also the raw LLDP port id, SNMP ifIndex and UniFi port index when matched, and `role` (access, uplink, unknown). |
| `infra_jacks` | `jack_key` | The app's site port id (`site/building/room/panel/NN`), else the user's label. Room, site and the current `(switch_id, port_key)` it is patched to. |
| `infra_links` | `id` | An edge between two of: port, port (uplink), jack, endpoint. Each edge has a source (`lldp`, `cdp`, `field_report`, `snmp_lldp`, `config`), first seen, last seen, and confidence. |
| `infra_endpoints` | `id` | A device seen on a port: a watchpost monitor, a pushed host, or a MAC and DHCP address learned in the field. |

### Custom properties with history

`port_properties(switch_id, port_key, name, value, unit, source, report_id, observed_at, recorded_at, recorded_by)`

- **Append-only.** Each field report adds rows, and the current value of a property is the newest row. The port page shows the history: who, when, and from which report.
- **Typed names.** Only a fixed set of property names is accepted: `jack_label`, `panel`, `room`, `site`, `cable_verdict`, `pair_1_2_length_m` (and the other pairs), `pair_fault`, `link_speed_mbps`, `duplex`, `poe_class`, `poe_voltage_v`, `poe_load_w`, `vlan`, `voice_vlan`, `dhcp_ok`, `dns_ok`, `last_tested_at` and `tester_serial`. Admins can also add free-form `custom.<name>` properties by hand. Those are audited.
- **Conflicts with live data are findings, never overwrites.** Comparisons are made against SNMP or UniFi data where available:
  - a field-verified speed above the live speed is a warning;
  - a field VLAN that differs from the live access VLAN is a warning;
  - PoE was verified, but the live port reports no power: warning;
  - a different switch or port than the last report: info, "re-patched".

### Field reports

`field_reports` keeps one row per uploaded report, keyed by `(source, report_id)`. It holds the summary columns and the stored body. Reports are the evidence; port properties and links are derived from them. Rebuilding the derived tables from reports is a supported admin operation.

## Building the map

1. **From a field report.**
   - The tester sits in a jack and sees an LLDP or CDP neighbour. That gives a jack-to-switch-port link, plus a switch with its management address.
   - The site port id gives the jack its room and panel.
2. **From live data**, as a later slice. An `snmp` monitor can read the switch's LLDP neighbour table (LLDP-MIB `lldpRemTable`). That gives port-to-port uplinks without anyone visiting the site.
3. **Matching to monitors.**
   - A switch matches a monitor when its management address or sysName equals the monitor's target.
   - A port matches an SNMP `interface` monitor by ifName or ifDescr.
   - A port matches a UniFi device port by device MAC and port index.
   - Matching only links things; it never creates a monitor.
4. **Aging.**
   - A link not confirmed for `map.stale_days` (default 90) is shown faded as stale.
   - A link not confirmed after twice that is hidden, but kept in history.
   - A report that places a jack on a different port closes the old link immediately.

## Availability and health

- **Live state for each node** comes from the monitors matched to it:
  - switches: SNMP or UniFi device, or a ping monitor;
  - ports: an SNMP interface monitor or the UniFi port state;
  - endpoints: pushed hosts and host monitors.
- **Health combines that live state with the newest field findings.**
  - A port with a passing live check but a new cable fault from the field is shown as Warning.
- **Dependencies are inferred, but nothing is hard-wired without review.**
  - The map proposes `depends_on` edges: an endpoint depends on its access switch, and an access switch on its uplink.
  - Edges with high confidence (LLDP seen in the last `stale_days`) can be applied automatically when `map.auto_depends: true`.
  - Otherwise they are listed for an admin to accept.
  - An applied edge feeds the existing rollup. When a switch goes DOWN, everything behind it shows UNREACHABLE through that switch, and only the switch alerts.

## Views

- **Map.** A layered diagram: core, distribution, access, then jacks and endpoints. The layers are drawn from the links, each node is coloured by health, and its state is spelled out in words so colour is never the only signal. You can filter by site and building.
- **Port page.** Live state and counters, the current properties, a property history table, the field reports for this port, findings with acknowledgement, and the matched monitors.
- **Jack page.** Room and panel, the switch and port it is patched to over time, and its reports.
- **Report detail.** The typed sections first, then the raw steps and tool results.

## Upload from the phone

- **Endpoint.** `POST /api/v1/field-reports` takes a new versioned report schema, not the hostwatch Batch.
  - **Units** are part of each field name.
  - **Idempotency:** a report uploaded again is recognised by its report id; an edited report carries a higher revision.
  - **Limit:** 256 KiB per report.
- **Authentication.** A per-phone key `wpf_<prefix>_<secret>` that can only upload reports. A host key cannot upload reports, and a report key cannot ingest host data.
- **Phone-side delivery.**
  - Reports go into a queue on the phone that survives restarts.
  - The queue is sent when the phone is on Wi-Fi or a VPN, with backoff between retries.
  - Nothing is queued unless you tap "Send to watchpost", or turn on automatic queueing in settings.
- **Privacy defaults.**
  - Location and Wi-Fi SSID and BSSID are sent by default (owner decision). Each can be turned off in the app.
  - Left out unless you opt in: survey and BLE data, the phone's own addresses, and the lists of LAN hosts it discovered.
  - SSH transcripts and script values are never sent.
  - Tester serial and LLDP, CDP and DHCP data are always sent.

## Threat model additions

- **A stolen phone or key.** The key can only upload. It is revoked per device and gives no read access.
- **Poisoned reports.** A forged report can create false findings or wrong links, so:
  - the key is bound to a source device and the UI shows which source each report came from;
  - links never create monitors;
  - automatic dependency edges need LLDP confirmation, or an admin's acceptance.
- **Plain HTTP on the LAN.** Accepted only for private addresses. TLS with certificate pinning is recommended.
- **Stored cross-site scripting.** Every string from a report is escaped on output.

## Owner decisions (2026-10-04)

- Inferred dependencies apply automatically when confirmed by LLDP or CDP within `map.stale_days` (90). Weaker edges wait for an admin to accept them.
- Upload is manual ("Send to watchpost"); automatic queueing of every saved report is an opt-in setting.
- Field findings are shown on the dashboard, port page and map only. They do not send alerts.
- Location and Wi-Fi names are sent with reports by default.
- A switch seen in the field that matches no monitor is created as an unlinked switch and queued for an admin to link.
- A gzip body is accepted only with a capped inflated size and compression ratio.

## Packaging: core map, Pockethernet as a plugin (owner decision 2026-10-04)

watchpost gains a plugin system, and this design is split between the core and the first plugin.

- **Core.**
  - Switches, ports and port properties with history, the infrastructure map, monitor matching, conflict findings and inferred dependencies.
  - SNMP, UniFi and pushed hosts feed the same map, so none of it is specific to Pockethernet.
- **Plugin host (core).** Plugins are found through Python entry points (group `watchpost.plugins`) and loaded only when listed under `plugins:` in `watchpost.yaml`. A plugin declares the core versions it supports, and a mismatch refuses to start. Through fixed hooks a plugin can register:
  - routers;
  - a key scope;
  - its own migrations, with a version number per plugin;
  - a config section;
  - pages and navigation entries;
  - monitor types;
  - map contributions: nodes, edges, property writes and suggested dependencies.
- **Core enforcement.** Every plugin route gets the core's authentication, CSRF, rate limiting and audit, so a plugin cannot bypass them.
- **Trust.** Plugins run in-process with full trust. Only plugins installed into the image and named in the config are loaded, and nothing is downloaded at runtime.
- **Pockethernet plugin** (package `watchpost-pockethernet`, kept in this repository under `plugins/pockethernet` until it needs its own):
  - the report schema and the `wpf` key scope;
  - the upload endpoint;
  - mapping from a report to port properties and edges;
  - the report and jack pages.
- **Later plugins.** hostwatch ingest and the control phase can move to plugins under the same rules. Control would then be entirely absent unless enabled.

### Built: plugin loader (slice 1)

`watchpost/plugins.py` and the mounting code in `watchpost/web.py` implement the host. Details differ from the sketch above in these ways:

- Settings live under `plugin_settings.<name>` next to `plugins:`, because the core config rejects unknown keys.
- Routers, the config section and navigation entries are active. Key scopes, monitor types and map contributions are declared and validated at load, and applied by the slices that build them (build order item 6 and the plugin's key scope work).
- Every plugin route needs a login session for now. A key-authenticated route, as the phone upload needs, comes with the key scope slice and will be mounted by the core in the same way.
- Audit kinds are `plugin_request` (state-changing requests), `plugin_denied` and `plugin_failed`. Successful reads are not audited.
- A plugin's monitor type names must start with `<plugin>.`, and key scope markers may not be `wpi`.

### Built: plugin migrations, config and pages (slice 2)

- `plugin_schema` (core schema version 5) holds one version per plugin. `Store` applies the migrations of listed plugins after the core's, one transaction per step, and raises `PluginSchemaTooNewError` when a plugin's recorded version is newer than its code. A plugin that is not listed is not touched, and its tables are kept when it is disabled.
- A plugin's settings are validated by its own model under `plugin_settings.<name>`. The error names the field and the reason, never the value.
- Pages and navigation entries exist only for listed plugins. A page path must be `/plugins/<name>` or below it. A plugin may also serve a `static_dir` at `/plugins/<name>/static`, behind the core's security headers and CSP.

### Built: infrastructure tables and property history (slice 4)

Schema version 6 and `watchpost/portkey.py` and `watchpost/infra.py` implement the data model. Details that the sketch above left open:

- `switch_id` is `mac:<12 hex digits>` or `name:<lower-cased sysName>`, so a sysName that looks like a MAC cannot collide with a chassis id. Port keys are lower case with no whitespace; LLDP port ids that are not names keep a prefix (`mac:`, `addr:`, `circuit:`, `pc:`).
- `infra_links` stores its two ends as sorted `(kind, ref)` pairs and is unique per ends and source. A port ref is `<switch_id>|<port_key>`, so neither may contain a vertical bar. `closed_at` is reserved for the ageing slice.
- `port_properties` has an extra `last_verified` column. A report that repeats the newest value bumps it and adds no row; the original `observed_at` stays.
- Property values are stored as JSON text with the type fixed per name (for example `link_speed_mbps` is an integer and `dhcp_ok` a boolean). `custom.<name>` values are text.
- The new `scope` column on `ingest_keys` is not part of this step; it comes with the key scope slice.

### Built: monitor matching and conflict findings (slice 5)

`watchpost/infra_match.py` implements the matching and findings. Details that the sketch above left open:

- A switch matches by chassis MAC, then management address, then sysName; the first key with candidates decides. The sketch said address or sysName equals the monitor's target; the chassis MAC was added for UniFi device monitors, whose `device` may be a MAC.
- The best monitor type wins (snmp, unifi_network, ping, tcp). A tie between monitors of that type matches nothing and the switch goes to the unlinked queue. SNMP interface monitors belong to ports and never match a switch.
- Matches are computed on each read and never written. `matched_monitor` holds only an admin link, which is kept while its monitor is configured, enabled or not.
- The unlinked queue is the set of switches with no admin link and no automatic match. `GET /api/admin/infra/unlinked` lists it, and `POST /api/admin/infra/link` with `switch_id` and `monitor` links one. Both need an admin session, the post needs the CSRF token, and the result is audited.
- Findings are `speed_above_live`, `vlan_mismatch`, `poe_no_power` and `repatched`, computed on each `GET /api/infra/findings`. Live values come from the last polled check detail. The SNMP interface check now reports `speed_mbps`. VLAN, PoE power and UniFi per-port values are used when a check reports them (`vlan`, `poe_w`, or `ports.<index>` for UniFi); until then those comparisons produce nothing, never a guess.
- `repatched` is derived from the `jack_label` property history: the newest row for a label is on a different port than an older row for the same label.
- Findings are never stored, never acknowledged yet (the port page slice adds that), and never call an alert target.

### Built: map data, ageing and inferred dependencies (slice 6)

`watchpost/infra_map.py` implements `GET /api/infra/map`, the ageing and the dependency plan. Schema version 7 adds `infra_dependencies`, which stores only an admin's decision. Details that the sketch above left open:

- **Map.** `GET /api/infra/map?site=&building=` (login session) returns `nodes` and `edges`. A node has an `id` (`switch:<id>`, `port:<id>|<key>`, `jack:<key>`, `endpoint:<n>`), a `kind`, a `label`, the matched `monitor` slug, and a live `state` that is one of `up`, `warn`, `down`, `unreachable`, `pending` or `unknown`, with `blocked_by` naming the ancestor behind an unreachable one. A port also carries its `parent` switch, `role` and `findings`; a port whose check passes but which has a field finding is shown as `warn`. Switches, and the ports that have a visible link or a patched jack, are nodes. Building is the second part of the app's site port id (`site/building/room/panel/NN`). A filter keeps the matching jacks, the ports they reach, the switches of those ports and the switches one uplink away; a site filter also keeps ports whose newest `site` property matches. Edges carry `source`, `confidence`, `state` (`active` or `stale`) and `age_days`.
- **Ageing.** Computed on read from `last_seen` and the clock. Older than `map.stale_days` is stale, older than twice that is hidden, and a link with `closed_at` is never shown. Rows are never deleted, and confirming a link again brings it back.
- **Contradiction.** `InfraService.upsert_link` closes the older link at once when a report puts a jack on a different port, or a port against a different neighbour port. Endpoints do not contradict one another, and `config` links are neither closed nor closing.
- **Proposals.** An endpoint whose monitor (or pushed host) sits on a switch port proposes a dependency on that switch's monitor. A port-to-port link proposes that the switch whose port has role `uplink` depends on the switch at the other end; when neither or both ends are uplinks the direction is unknown and nothing is proposed. Both sides must match an enabled monitor. A proposal is strong when its link source is `lldp`, `cdp` or `snmp_lldp` and it was confirmed within `stale_days`; every other proposal is weak.
- **Applying.** With `map.auto_depends` (default true) strong proposals are applied; weak ones are pending. `POST /api/admin/infra/depends/accept` and `/reject` (admin session, CSRF token, body `child` and `parent` monitor slugs) record a decision, audited as `infra_depends_accepted`, `infra_depends_rejected` or `infra_depends_failed`. A rejection holds even for a strong proposal, and an acceptance is applied before the automatic edges. `GET /api/infra/dependencies` lists `applied`, `pending`, `refused` (with the reason), `rejected` and the `configured` YAML edges.
- **Effective set.** `Config.parents` returns the YAML parents plus the applied edges, so the rollup and the scheduler's parent confirmation use both. The applied edges live in memory, are recomputed from the links on every map or dependency read and once a minute by a scheduler hook, and are never written to the YAML. A contradicted, closed or hidden link removes its edge.
- **Cycles.** An edge that would close a cycle with the YAML and the edges already applied is refused, listed under `refused`, and cannot be accepted.

## Build order

**watchpost core.**
1. Plugin loader and hooks (built).
2. Per-plugin migrations (built).
3. Plugin pages and navigation (built).
4. Infrastructure tables, port-key normalisation and property history (built).
5. Monitor matching and conflict findings (built).
6. Map data, ageing and inferred dependencies (built).
7. Map and port pages.

**Pockethernet plugin.**
1. Report schema.
2. Key scope.
3. Upload endpoint.
4. Report to properties and edges.
5. Report and jack pages.
6. Findings on the dashboard.

**Later.** LLDP-MIB polling for uplinks (core).

**Then the app.**
1. Payload mapper with golden files shared with watchpost.
2. Settings, with the key stored in the Android Keystore.
3. Client.
4. Persistent outbox.
5. UI: preview, send now, upload status.
6. Optional automatic queueing.
