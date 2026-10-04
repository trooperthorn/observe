# Field data from the Pockethernet app

Status: design, not built. This document describes how test results from the
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
- **Authentication.** A per-phone key `wpr_<prefix>_<secret>` that can only upload reports. A host key cannot upload reports, and a report key cannot ingest host data.
- **Phone-side delivery.**
  - Reports go into a queue on the phone that survives restarts.
  - The queue is sent when the phone is on Wi-Fi or a VPN, with backoff between retries.
  - Nothing is queued unless you tap "Send to watchpost", or turn on automatic queueing in settings.
- **Privacy defaults.** These are left out unless you opt in:
  - location;
  - Wi-Fi SSID and BSSID;
  - survey and BLE data;
  - the phone's own addresses;
  - the lists of LAN hosts it discovered.

  SSH transcripts and script values are never sent. Tester serial and LLDP, CDP and DHCP data are always sent.

## Threat model additions

- **A stolen phone or key.** The key can only upload. It is revoked per device and gives no read access.
- **Poisoned reports.** A forged report can create false findings or wrong links, so:
  - the key is bound to a source device and the UI shows which source each report came from;
  - links never create monitors;
  - automatic dependency edges need LLDP confirmation, or an admin's acceptance.
- **Plain HTTP on the LAN.** Accepted only for private addresses. TLS with certificate pinning is recommended.
- **Stored cross-site scripting.** Every string from a report is escaped on output.

## Build order

**watchpost first.**
1. Migration.
2. Report schema.
3. `wpr` keys.
4. Upload endpoint.
5. Port, jack and property derivation, with history.
6. Findings: field changes and conflicts with live data.
7. Monitor matching.
8. Map data and inferred dependencies.
9. Views.
10. Alerts.
11. LLDP-MIB polling for uplinks.

**Then the app.**
1. Payload mapper with golden files shared with watchpost.
2. Settings, with the key stored in the Android Keystore.
3. Client.
4. Persistent outbox.
5. UI: preview, send now, upload status.
6. Optional automatic queueing.
