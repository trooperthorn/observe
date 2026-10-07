# Golden OpenTelemetry fixtures from HA SOC

These files are a contract fixture. They are copied unchanged from `tests/fixtures/observe_otlp` in the ha_Int_soc repository (branch `staged/soc-fixes-oct`), where the directory was last changed by commit `b8f4997` ("Send distinct integration series, skip dry-run bundles and track watchdog episodes correctly"). The branch head when they were copied was `41cef7f`.

`metrics.json` is the OTLP metrics request HA SOC sends, `logs.json` the OTLP logs request (watchdog breaches and `observe.ha.crash` records) and `snapshot.json` the HA SOC state they were built from. Observe does not edit these files. `tests/test_ha_push.py` sends them through the real `/v1/metrics` and `/v1/logs` routes and checks the Home Assistant, containers, integrations, repairs and backups sections, the crash classification and the platform. When HA SOC changes its mapping, copy the new files here, update the commit above, and let the test show what Observe must follow.
