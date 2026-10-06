# Field data from the Pockethernet app

Status: design; the Pockethernet report schema and key scope, the plugin loader, per-plugin migrations, plugin pages, the upload endpoint, the infrastructure tables with port keys and property history, monitor matching with conflict findings, map data with ageing and inferred dependencies, and the map, port and map admin pages are built, as are the mapping from reports to port properties and map edges and its rebuild; the report list, report detail and jack pages and the field change findings on the dashboard, port page and map are built too. Owner decisions recorded 2026-10-04 are in the last section. This document describes how test results from the
Pockethernet Android app (repo `pocketethernet-app`) become properties of switch
ports in Observe, and how the mapping data in those results builds an
infrastructure map with live availability and health.

## Goals

1. Field-verified facts about a port become custom properties on that port:
   wall jack and patch panel label, room and site, cable result and length per
   pair, verified link speed and duplex, PoE class and power, VLAN and voice
   VLAN, DHCP and DNS result, and when and with which tester it was last tested.
2. LLDP and CDP neighbour data, the app's site model (site, building, room,
   panel, port) and its port map build a topology of switches, ports, jacks,
   endpoints and uplinks. Observe overlays live state from its existing
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
| `infra_endpoints` | `id` | A device seen on a port: an Observe monitor, a pushed host, or a MAC and DHCP address learned in the field. |

### Custom properties with history

`port_properties(switch_id, port_key, name, value, unit, source, report_id, observed_at, recorded_at, recorded_by)`

- **Append-only.** Each field report adds rows, and the current value of a property is the row with the newest `observed_at`, not the row written last. A late or resent older observation adds a history row and never replaces a newer current value. The port page shows the history: who, when, and from which report.
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
   - A switch is matched to a monitor automatically only by its LLDP chassis id or an admin-confirmed link. A management address or sysName that equals the monitor's target is a proposal that an admin confirms.
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
  - Nothing is queued unless you tap "Send to Observe", or turn on automatic queueing in settings.
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
- Upload is manual ("Send to Observe"); automatic queueing of every saved report is an opt-in setting.
- Field findings are shown on the dashboard, port page and map only. They do not send alerts.
- Location and Wi-Fi names are sent with reports by default.
- A switch seen in the field that matches no monitor is created as an unlinked switch and queued for an admin to link.
- A gzip body is accepted only with a capped inflated size and compression ratio.

## Packaging: core map, Pockethernet as a plugin (owner decision 2026-10-04)

Observe gains a plugin system, and this design is split between the core and the first plugin.

- **Core.**
  - Switches, ports and port properties with history, the infrastructure map, monitor matching, conflict findings and inferred dependencies.
  - SNMP, UniFi and pushed hosts feed the same map, so none of it is specific to Pockethernet.
- **Plugin host (core).** Plugins are found through Python entry points (group `observe.plugins`, with the legacy group `watchpost.plugins` still read and warned about for compatibility) and loaded only when listed under `plugins:` in `observe.yaml`. A plugin declares the core versions it supports, and a mismatch refuses to start. Through fixed hooks a plugin can register:
  - routers;
  - a key scope;
  - its own migrations, with a version number per plugin;
  - a config section;
  - pages and navigation entries;
  - monitor types;
  - map contributions: nodes, edges, property writes and suggested dependencies.
- **Core enforcement.** Every plugin route gets the core's authentication, CSRF, rate limiting and audit, so a plugin cannot bypass them. A key-authenticated route counts valid requests per key and per peer, and counts failed or missing keys per peer separately, each against `server.plugin_rate_per_minute`, so bad-key traffic cannot use up a valid key's allowance.
- **Trust.** Plugins run in-process with full trust. Only plugins installed into the image and named in the config are loaded, and nothing is downloaded at runtime.
- **Pockethernet plugin** (package `observe-pockethernet`, kept in this repository under `plugins/pockethernet` until it needs its own):
  - the report schema and the `wpf` key scope;
  - the upload endpoint;
  - mapping from a report to port properties and edges;
  - the report and jack pages.
- **Later plugins.** hostwatch ingest and the control phase can move to plugins under the same rules. Control would then be entirely absent unless enabled.

### Built: plugin loader (slice 1)

`observe/plugins.py` and the mounting code in `observe/web.py` implement the host. Details differ from the sketch above in these ways:

- Settings live under `plugin_settings.<name>` next to `plugins:`, because the core config rejects unknown keys.
- Routers, the config section and navigation entries are active. Key scopes, monitor types and map contributions are declared and validated at load, and applied by the slices that build them (build order item 6 and the plugin's key scope work).
- Plugin routes need a login session, except a router that declares a key scope, which the core authenticates with a key of that scope (see the upload endpoint slice).
- Audit kinds are `plugin_request` (state-changing requests), `plugin_denied` and `plugin_failed`. Successful reads are not audited.
- A plugin's monitor type names must start with `<plugin>.`, and key scope markers may not be `wpi`.

### Built: plugin collectors hook

- `PluginBase.collectors()` returns `Collector(name, run, interval, timeout)` objects. `run` is an async callable that takes the store. Startup refuses an interval below 30 seconds, a timeout that is not above 0 or is longer than the interval, a non-async callable, and a repeated name.
- The scheduler (`Scheduler.add_collectors`, `_collector_loop`) runs each collector in its own task, once at startup and then after each interval. A timeout cancels the run. An exception or timeout is logged once per streak of failures, with one line when it recovers, and never reaches other collectors or the scheduler.

### Built: plugin migrations, config and pages (slice 2)

- `plugin_schema` (core schema version 5) holds one version per plugin. `Store` applies the migrations of listed plugins after the core's, one transaction per step, and raises `PluginSchemaTooNewError` when a plugin's recorded version is newer than its code. A plugin that is not listed is not touched, and its tables are kept when it is disabled.
- A plugin's settings are validated by its own model under `plugin_settings.<name>`. The error names the field and the reason, never the value.
- Pages and navigation entries exist only for listed plugins. A page path must be `/plugins/<name>` or below it. A plugin may also serve a `static_dir` at `/plugins/<name>/static`, behind the core's security headers and CSP.

### Built: infrastructure tables and property history (slice 4)

Schema version 6 and `observe/portkey.py` and `observe/infra.py` implement the data model. Details that the sketch above left open:

- `switch_id` is `mac:<12 hex digits>` or `name:<lower-cased sysName>`, so a sysName that looks like a MAC cannot collide with a chassis id. Port keys are lower case with no whitespace; LLDP port ids that are not names keep a prefix (`mac:`, `addr:`, `circuit:`, `pc:`).
- `infra_links` stores its two ends as sorted `(kind, ref)` pairs and is unique per ends and source. A port ref is `<switch_id>|<port_key>`, so neither may contain a vertical bar. `closed_at` is reserved for the ageing slice.
- `port_properties` has an extra `last_verified` column. A report that repeats the current value sets it to that report's observation time (never moving it back) and adds no row; the original `observed_at` stays. An older observation never changes the current row. A value is compared with the newest row of the same source only, so the UniFi feed (source `unifi`) and a field test each keep their own history and one never confirms the other.
- Property values are stored as JSON text with the type fixed per name (for example `link_speed_mbps` is an integer and `dhcp_ok` a boolean). `custom.<name>` values are text.
- The new `scope` column on `ingest_keys` is not part of this step; it comes with the key scope slice.

### Built: monitor matching and conflict findings (slice 5)

`observe/infra_match.py` implements the matching and findings. Details that the sketch above left open:

- A switch is matched automatically by chassis MAC (a UniFi device monitor whose `device` is that MAC) or by an admin link. A management address or sysName equal to the monitor's target is only a proposal, because a report can claim any address or name. The proposal is never applied, so it gives the monitor's live state to no switch until an admin confirms it. `GET /api/admin/infra/unlinked` lists each queued switch with its `proposed_monitor`, and confirming it uses the link route; the audit row records `basis` as `proposal` or `manual`.
- For a proposal the best monitor type wins (snmp, unifi_network, ping, tcp). A tie between monitors of that type proposes nothing and the switch goes to the unlinked queue without a proposal. SNMP interface monitors belong to ports and never match a switch.
- Matches are computed on each read and never written. `matched_monitor` holds only an admin link, which is kept while its monitor is configured, enabled or not.
- The unlinked queue is the set of switches with no admin link and no automatic match. `GET /api/admin/infra/unlinked` lists it, and `POST /api/admin/infra/link` with `switch_id` and `monitor` links one. Both need an admin session, the post needs the CSRF token, and the result is audited.
- Findings are `speed_above_live`, `vlan_mismatch`, `poe_no_power` and `repatched`, computed on each `GET /api/infra/findings`. Live values come from the last polled check detail. The SNMP interface check now reports `speed_mbps`. With `host_name` set it also stores `if_up`, rates, speed and utilization on that host's page; the interface detail now carries `name` and `oper_status`, and the map reads only `speed_mbps` as before. VLAN, PoE power and UniFi per-port values are used when a check reports them (`vlan`, `poe_w`, or `ports.<index>` for UniFi); until then those comparisons produce nothing, never a guess.
- `repatched` is derived from the `jack_label` property history: the newest row for a label is on a different port than an older row for the same label.
- Findings are never stored, never acknowledged yet (the port page slice adds that), and never call an alert target.

### Built: map data, ageing and inferred dependencies (slice 6)

`observe/infra_map.py` implements `GET /api/infra/map`, the ageing and the dependency plan. Schema version 7 adds `infra_dependencies`, which stores only an admin's decision. Details that the sketch above left open:

- **Map.** `GET /api/infra/map?site=&building=` (login session) returns `nodes` and `edges`. A node has an `id` (`switch:<id>`, `port:<id>|<key>`, `jack:<key>`, `endpoint:<n>`), a `kind`, a `label`, the matched `monitor` slug, and a live `state` that is one of `up`, `warn`, `down`, `unreachable`, `pending` or `unknown`, with `blocked_by` naming the ancestor behind an unreachable one. A port also carries its `parent` switch, `role` and `findings`; a port whose check passes but which has a field finding is shown as `warn`. Switches, and the ports that have a visible link or a patched jack, are nodes. Building is the second part of the app's site port id (`site/building/room/panel/NN`). A filter keeps the matching jacks, the ports they reach, the switches of those ports and the switches one uplink away; a site filter also keeps ports whose newest `site` property matches. Edges carry `source`, `confidence`, `state` (`active` or `stale`) and `age_days`.
- **Ageing.** Computed on read from `last_seen` and the clock. Older than `map.stale_days` is stale, older than twice that is hidden, and a link with `closed_at` is never shown. Rows are never deleted, and confirming a link again brings it back. Ageing uses the observation time of the confirming report, so a late or resent older report neither refreshes a link nor reopens one that a newer observation closed, and a jack is only re-patched by an observation at least as new as its last one.
- **Contradiction.** `InfraService.upsert_link` closes the older link at once when a report puts a jack on a different port, or a port against a different neighbour port. Endpoints do not contradict one another, and `config` links are neither closed nor closing.
- **Proposals.** An endpoint whose monitor (or pushed host) sits on a switch port proposes a dependency on that switch's monitor. A port-to-port link proposes that the switch whose port has role `uplink` depends on the switch at the other end; when neither or both ends are uplinks the direction is unknown and nothing is proposed. Both sides must match an enabled monitor. A proposal is strong when its link source is `lldp`, `cdp` or `snmp_lldp` and it was confirmed within `stale_days`; every other proposal is weak.
- **Applying.** With `map.auto_depends` (default true) strong proposals are applied; weak ones are pending. `POST /api/admin/infra/depends/accept` and `/reject` (admin session, CSRF token, body `child` and `parent` monitor slugs) record a decision, audited as `infra_depends_accepted`, `infra_depends_rejected` or `infra_depends_failed`. A rejection holds even for a strong proposal, and an acceptance is applied before the automatic edges. `GET /api/infra/dependencies` lists `applied`, `pending`, `refused` (with the reason), `rejected` and the `configured` YAML edges.
- **Effective set.** `Config.parents` returns the YAML parents plus the applied edges, so the rollup and the scheduler's parent confirmation use both. The applied edges live in memory, are recomputed from the links on every map or dependency read and once a minute by a scheduler hook, and are never written to the YAML. A contradicted, closed or hidden link removes its edge.
- **Cycles.** An edge that would close a cycle with the YAML and the edges already applied is refused, listed under `refused`, and cannot be accepted.

### Built: map, port and map admin pages (slice 7)

`observe/infra_port.py`, three static pages and schema version 8 implement the views for the core map. Details that the sketch above left open:

- **Pages.** `/map`, `/port?switch_id=&port=` and `/admin/infra` are static files with no data. Their scripts send a visitor without a session to `/login`, and the admin page shows nothing without an admin session. All text is written with `textContent`, and there is no `innerHTML`, inline script or inline style.
- **Map page.** Switches are placed by uplink depth from the map's port links: a switch with nothing above it but switches below is core, the deepest is access, and anything between is distribution. A switch with no uplink links is shown as access. Each node shows its state in words (Up, Warning, Down, Unreachable with the blocking ancestor, Pending, State unknown) as well as colour. Stale links are dashed, and a links table repeats every edge as text. The site and building filters are filled from the jacks in the unfiltered map.
- **Port page.** `GET /api/infra/port` returns live values and state of the matched monitors, the current properties, up to 50 history rows per property, and the findings for the port. The field report list, report detail and jack pages are in the Pockethernet plugin (see "Built: report and jack pages and field change findings").
- **Acknowledging findings.** `POST /api/admin/infra/findings/ack` (admin session, CSRF token, body `switch_id`, `port_key`, `kind`) stores the acknowledged message in `infra_finding_acks`. It is audited as `infra_finding_acknowledged` or `infra_finding_ack_failed`. A finding that no longer exists cannot be acknowledged, and when the facts change the message changes and the finding shows as new. An acknowledged warning no longer turns a passing port to Warning. Acknowledging sends no alert.
- **Map admin page.** It lists the unlinked switch queue with a choice of snmp, unifi_network, ping or tcp monitors, and the pending, rejected and refused dependency proposals. Accept and Reject use the existing routes; a rejected proposal can still be accepted.

### Built: Pockethernet report schema and key scope (plugin slices 1 and 2)

The package `observe_pockethernet` in `plugins/pockethernet` (entry point `pockethernet` in group `observe.plugins`, supported core versions `>=2026.9,<2027`) holds `schema.py` and `keys.py`. Core schema version 9 adds `ingest_keys.scope`. Details that the sketch above left open:

- **Schema.** `pockethernet.report` version 1 has the envelope `schema`, `version`, `report_id`, `revision` and `taken_at_ms`, then `device`, `site` (the app's site model), `geo`, `wifi`, `steps`, `neighbors` (LLDP and CDP), `dhcp`, `link`, `poe`, `properties` and `tool_results`. Names carry units (`speed_mbps`, `pair_1_2_length_m`, `poe_load_w`, `lease_s`, `latitude_deg`, times in `_ms`). `last_tested_at` is carried as `last_tested_at_ms`, and the mapping slice turns it into the property.
- **Strict.** Unknown fields are rejected at every level, because the phone and Observe ship together. `properties` is the allowlist from the data model section, one typed field per name; `custom.<name>` is never accepted from a phone.
- **Never accepted.** A key named `transcript`, `script_runs`, `scriptRuns`, `script_values` or similar, at any depth, rejects the whole report with a message that does not repeat the value.
- **Caps.** 256 KiB per body (413), JSON nesting 16 (400), strings 1024 characters (names 128, notes 4096), 64 steps and fields, 8 neighbours, 32 tool results, 16 addresses per list. Control characters are refused (notes may keep a newline), and NaN and the infinities are refused, both as JSON literals and as floats.
- **Location and Wi-Fi** are accepted, as decided. They are stored with the report and never become port properties.
- **Key scope.** `wpf_<prefix>_<secret>` keys are stored in `ingest_keys` with scope `wpf`, bound to a device label in the host column. The core checks the marker and the stored scope, so `verify_key` and `key_host` for `wpi` refuse a `wpf` key and the plugin's `verify_field_key` refuses a `wpi` key. Admins issue them from the admin create route with `scope: "wpf"` or with `--ingest-key-scope wpf`, and only when the plugin is listed.
- **Not built yet.** The plugin is not yet copied into the Docker image.

### Built: upload endpoint (plugin slice 3)

`upload.py` and `reports.py` in the plugin, and a key-authenticated router option in the core, implement the upload. Details that the sketch above left open:

- **Core hook.** `PluginRouter(router, key_scope="wpf", public_prefix="/api/v1")`. The core applies a bearer key check for that scope and the rate limits described above before the body is read, and audits the request, so the earlier note that every plugin route needs a session no longer holds for such a router. A plugin may only name its own scope, never `admin` with it, and `public_prefix` must be `/api/v1` or below. A `prune` hook lets a plugin apply retention; the core calls it about hourly.
- **Routes.** `POST /api/v1/field-reports` and `GET /api/v1/field-reports/ping` (returns the device label, the server time, the size cap and the accepted encodings). The mount point is the one in the sketch, not `/api/plugins/pockethernet/`.
- **Order.** Rate limit (429), key (401), body cap 256 KiB on the wire (413), content encoding (415), gzip inflate, schema (400, 413 or 422), clock, store.
- **Gzip.** One gzip member only. Output is capped at 256 KiB (413) and at 50 times the compressed size when above 16 KiB (413). Truncated, trailing or non-gzip data is 400. The stored body is the inflated JSON.
- **Idempotency.** Table `field_reports`, keyed by `(source, report_id)`, with the source being the key's device label (a deviation from "by report id alone": another phone's key cannot replace or hide a report). Higher revision replaces (`replaced`), equal is `duplicate`, lower is `ignored`; all answer 200 so the phone's outbox drops the item.
- **Clock.** The sketch said only "correction beyond 300 s". A phone cannot be corrected from its own report time alone, so the phone may send `X-Report-Sent-Ms` (its clock at send). A difference over 300 s from the server clock is added to the report time. A report time more than 300 s ahead of the server is set to now regardless. `clock_corrected` is returned and stored, with both times kept. A bad header value is 400.
- **Audit.** Every state-changing request with a valid key is a `plugin_request` row (actor is the key prefix, detail has the device, result, report id, revisions, `clock_corrected` and size, or the reason for a refusal); 401 and 429 are `plugin_denied`. No key and no report content is written.
- **Retention.** `plugin_settings.pockethernet.evidence_retention_days` (default 365, 1 to 3650) drops the body of a report not updated for that long and keeps the summary row.

### Built: report to properties and edges (plugin slice 4)

`derive.py` in the plugin turns each accepted or replaced report into core data through `InfraService`, and `POST /api/plugins/pockethernet/rebuild` repeats it from the stored bodies. Details that the sketch above left open:

- **Neighbour.** LLDP is preferred over CDP, and the first neighbour that names both a switch and a port is used. The switch is the LLDP chassis MAC when the chassis id or device id is a MAC, else the system name (for CDP, the device id). The port is read with the LLDP port id subtype when there is one, else normalised as a plain name. A report with no such neighbour is stored and derives nothing; the audit row says `skipped`.
- **Entities.** The switch gets its name, management addresses, vendor and platform. The port is created with role `access`. The jack key is the site port id, else the location label, else the `jack_label` property. The jack is patched to the port and joined to it by a `field_report` link (confidence 0.9 for LLDP, 0.8 for CDP). No other kind of link or endpoint is written, so a report alone never produces an edge that the dependency plan treats as strong.
- **Re-patching.** A report that puts the jack on another port closes the earlier link at once, through the core's contradiction rule. The old port keeps its properties and history.
- **Properties.** Every allowlisted property in the report is appended to the port. `jack_label`, `panel`, `room` and `site` fall back to the site model, `tester_serial` to the device serial, and `last_tested_at` to the corrected report time. A `last_tested_at_ms` sent by the phone is shifted by the same skew correction as the report time. `last_tested_at_ms` becomes `last_tested_at` in seconds, and `poe_class` and `tester_serial` are stored as text, as the core defines them. Units are `m`, `Mbps`, `V`, `W` and `s`. A value equal to the current one only moves `last_verified`.
- **Provenance.** Each row has source `pockethernet`, the report id, `observed_at` from the corrected report time, `recorded_at` from the time the report was received, and `recorded_by` of `<key prefix>:<device label>`. The tester is the `tester_serial` property. Plugin schema version 2 adds `field_reports.key_prefix` so a rebuild can credit the same key.
- **Failure.** A failed derivation never loses the evidence, but it is not answered as a clean accept. The report is stored with `derive_status` `failed` (plugin schema version 3; `pending` between storing and deriving, `ok` once derived), the response is 202 with `derive_status: failed`, and the audit row has `derive_failed` with the error class only. `POST /api/plugins/pockethernet/retry` (admin session and CSRF token, audited with counts) derives every report that is not `ok` again, and a rebuild does too.
- **Revisions.** A higher revision deletes the earlier revision's properties (same report id and `recorded_by`) in the same transaction that replaces the stored report, so live state equals what a rebuild produces and a property only the earlier revision set is gone. The earlier revision's history is not kept.
- **Observation order.** Every map time the plugin writes (switch, port and jack `last_seen`, link confirmation, `observed_at`, `last_verified`) is the corrected observation time, not the receive time, which fills only `recorded_at`. An older report received after a newer one adds history but leaves the current values, the jack patch and link ageing unchanged; a link to the older port is closed at once.
- **Incomplete runs.** A report with status `cancelled` or `aborted` is stored as evidence and listed, but derives no switch, port, jack, link or property, because a partial run can hold zero speeds and half-read values. The audit row says the derivation was skipped. Only `complete` reports derive.
- **Rebuild.** Admin session and CSRF token, audited as `plugin_request` with the counts. In one transaction it deletes the properties with source `pockethernet` and the `field_report` links and unpatches those jacks, and then it replays every stored report in the order observed (corrected report time), with the original receive time kept as `recorded_at`, so the result is the same apart from row ids. Switches, ports and jacks are kept because other sources may share them. If any stored body fails to parse or derive, the whole transaction is rolled back, the old derived data stays, and the response is 409 listing the failed reports (also in the audit row). Only the newest stored revision of each report is replayed, so a superseded revision's history is not recreated. When retention has dropped any report body the rebuild is refused (409) before anything is deleted, because those reports could not be replayed.

### Built: report and jack pages and field change findings (plugin slices 5 and 6)

`pages.py`, three page files and `static/pockethernet.js` in the plugin, and `observe/infra_changes.py` in the core. Details that the sketch above left open:

- **Pages.** The plugin registers `/plugins/pockethernet` (report list), `/plugins/pockethernet/report?source=&report_id=` (report detail) and `/plugins/pockethernet/jack?key=` (jack) through the core's page hook, and one navigation entry, "Field reports", which the dashboard shows from `GET /api/plugins`. The pages are static shells with no data and sit outside the plugin's static folder. Their data routes are `GET /api/plugins/pockethernet/reports`, `/report` and `/jack`, which the core mounts behind a login session, so a visitor without one gets 401 and the script sends them to `/login`. When basic auth is configured the page shells ask for it as well, like `/host`. All text is written with `textContent`, and there is no `innerHTML`, inline script or inline style. The package data lists the page and script files so an install carries them.
- **Report list.** Newest first, 50 a page (at most 200), with the corrected taken time, source, jack, status, revision, tester serial, the ports the report produced and a link. A report whose body retention dropped still shows its summary.
- **Report detail.** The ports the report produced, then the typed sections (where, verdict and properties, link, PoE, DHCP, neighbours, tester, warnings, location, Wi-Fi), then the raw steps and tool results. A dropped body shows the summary and says so.
- **Jack page.** Room and site, the port the jack is patched to now, the patch history from its `jack_label` rows (newest first), its links with open or closed state, and its reports.
- **Field change findings.** Computed with the other findings on each read, from the last two values of a property in the port's history, whatever source wrote them. A property with one value is a baseline and produces nothing. A change stays until the value changes again, so a later report that restores the value clears it. At most one finding per kind per port.

  | Kind | Severity | Fires when |
  |---|---|---|
  | `speed_drop` | warning | `link_speed_mbps` is lower than before |
  | `cable_fault` | warning | `pair_fault` names a fault that differs from the earlier value |
  | `length_change` | warning | a pair length moved by more than 2 m |
  | `poe_drop` | warning | `poe_class` is lower, or `poe_load_w` fell below half of a positive earlier load |
  | `vlan_change` | info | `vlan` or `voice_vlan` differs |
  | `dhcp_fail` | warning | `dhcp_ok` went from true to false |
  | `verdict_worse` | warning | `cable_verdict` moved from pass to warn or fail, or warn to fail |

- **Where they show.** `GET /api/infra/findings` feeds a "Field findings" list on the dashboard, with a link to each port. The port page and the map use the same findings. An unacknowledged warning turns a port whose live check passes to Warning on both the port page and the map; an info finding or an acknowledged warning does not. This also changed the map: before, any finding turned a passing port to Warning, ignoring severity and acknowledgement.
- **No alerts.** Nothing in the findings code reaches the alerter, and a test fails if an alert target is called while findings are produced.

## Build order

**Observe core.**
1. Plugin loader and hooks (built).
2. Per-plugin migrations (built).
3. Plugin pages and navigation (built).
4. Infrastructure tables, port-key normalisation and property history (built).
5. Monitor matching and conflict findings (built).
6. Map data, ageing and inferred dependencies (built).
7. Map and port pages (built).

**Pockethernet plugin.**
1. Report schema (built).
2. Key scope (built).
3. Upload endpoint (built).
4. Report to properties and edges (built).
5. Report and jack pages (built).
6. Findings on the dashboard (built).

**Later.** LLDP-MIB polling for uplinks (core).

**Then the app.**
1. Payload mapper with golden files shared with Observe.
2. Settings, with the key stored in the Android Keystore.
3. Client.
4. Persistent outbox.
5. UI: preview, send now, upload status.
6. Optional automatic queueing.

- **UniFi map feed.** Each UniFi device is a switch keyed by chassis MAC (name, address, vendor `Ubiquiti`, model). A device that names its uplink device gets a `config` link between a placeholder port `uplink` on the child and `to-<child mac>` on the parent. The optional classic collector (only with `classic_credential`) adds the ports as `Port N` with `unifi_index` N (`unifi_port_key`, so "Port 5" and index 5 are one key, per switch), the properties `link_speed_mbps` (port up only), `poe_class`, `poe_load_w` and `vlan` with source `unifi`, written only when the value differs from the last `unifi` row (otherwise `last_verified` moves), a `config` link for the uplink port numbers, and an `lldp` link for each neighbour that is a UniFi device of the poll or an already known switch. Unknown neighbours are not created. A real port link closes the placeholder. Findings treat source `unifi` as live state: where the monitor check reports no VLAN, PoE watts or speed, a `unifi` property confirmed within 900 seconds is used, so `vlan_mismatch`, `poe_no_power` and `speed_above_live` work against Pockethernet data, and with no classic data they stay silent. Unverified field shapes: `speed`, `poe_class`, `poe_power`, `native_vlan`, `lldp_table`, `uplink` and port numbers on the classic API, and the uplink device id on Integration device rows.
- **UniFi clients and Protect cameras.** `unifi_clients` is one row per MAC: name, address, kind (`wired` or `wireless`, empty when unknown), the device it is on (`uplink_device_id`, `uplink_mac`), switch port, SSID, `connected` (NULL when unknown), connected since, whether classic detail was applied, first seen and last seen. It is polled every 300 seconds from the Integration API (verified fields: `id`, `name`, `macAddress`, `ipAddress`, `connectedAt`, `type`, `uplinkDeviceId`). The optional classic client adds the access point or switch, port and SSID of connected clients and adds known clients that are offline with the console's last seen time. A client that is no longer listed is marked not connected, and a row unseen for 30 days is deleted. Unverified: the values of `type`, the format of `connectedAt`, and the classic fields `ap_mac`, `sw_mac`, `sw_port`, `essid`, `is_wired`, `hostname` and `last_seen` (seconds). `unifi_cameras` is one row per Protect camera: name, MAC, model, state, `connected` and `recording`, polled every 120 seconds when `protect` is true from the unpaginated `GET /cameras` (verified as an unpaginated array). `connected` and `recording` are NULL unless the console gives a boolean `isConnected` or `isRecording`; the `recordingSettings.mode` form is unverified and not used. No NVR storage route is verified, so none is read. The UniFi page under Network shows both. The page marks a connected client or camera as Stale when its last seen time is older than 2.5 poll intervals, so a stopped collector is not shown as current.
- **UniFi ports mode.** The `unifi_network` check in mode `ports` reads one device detail and reports `detail.ports` keyed by port index, which feeds the live-port lookup for the map. Speed comes from `speedMbps`; VLAN and PoE watts are unknown (shown as unknown, never zero) because the Integration API does not provide them. The `interfaces.ports` field names are unverified against a live console.

- **UniFi inventory plugin.** The `unifi` plugin keeps the current devices of one UniFi site in `unifi_devices` (site, id, MAC, name, model, state, IP, firmware, a firmware update flag that is NULL when unknown, first seen, last seen), polled every 120 seconds from the Integration API. `unifi_clients` and `unifi_cameras` are described in the next item. Records unseen for 30 days are deleted. Field names come from ha_Int_soc `docs/UNIFI-LOCAL-API-CONTRACT.md`; the update flag on the list row is unverified against a live console. The map feed is described in the next item. The optional classic client (`classic_credential`) can read PoE watts and class, per-port native and tagged VLAN, the LLDP neighbour table, uplink port numbers, WAN health and offline clients through `classic_snapshot()`; the offline clients, port numbers and SSIDs are stored in `unifi_clients` and the port values feed the map; WAN health is parsed but not stored or shown, and every classic field name (`port_table`, `poe_power`, `lldp_table`, `uplink`, `stat/health` wan row) is unverified against a live console.
- **Which UniFi API each port field comes from.** Port facts are split between two APIs, and the infrastructure map keeps the source of each value.

  | Port field | Source | API call | Verified |
  |---|---|---|---|
  | Port index, link state, speed | Integration API | `GET /devices/{id}` (`interfaces.ports`, check mode `ports`) | Shape unverified against a live console |
  | Maximum speed, PoE state | Integration API | same call | Shape unverified |
  | PoE watts and class | Classic controller | `stat/device` (`port_table`, `poe_power`, `poe_class`) | Unverified |
  | Native and tagged VLAN | Classic controller | `stat/device` (`native_vlan`, `tagged_vlan_mgmt`) | Unverified |
  | LLDP neighbours | Classic controller | `stat/device` (`lldp_table`) | Unverified |
  | Uplink device and port number | Integration API (device) and classic `uplink` | device list, `stat/device` | Unverified |
  | Connected client port and SSID | Classic controller | `stat/sta` (`sw_port`, `essid`) | Unverified |
  | Offline clients | Classic controller | `rest/user` | Unverified |
  | WAN health | Classic controller | `stat/health` (parsed, not stored) | Unverified |

  The Integration API gives no VLAN or PoE watts, so without the classic account those values show as unknown, never zero.
