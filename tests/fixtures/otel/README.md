# Golden OpenTelemetry fixtures from the hostwatch agent

These files are a contract fixture. They are copied unchanged from `tests/fixtures/otel` in the hostwatch repository (branch `staged/otlp`), where each file was last changed by commit `a3b3bfe` ("Map every hostwatch collector to OpenTelemetry names, units and attributes"). The branch head when they were copied was `0aeb0c8`.

Each collector file has two lists. `samples` is what the collector reports and `points` is the OpenTelemetry point the agent's mapping (`hostwatch/otel_map.py`) produces for it. `events.json` has the agent events and the OTLP log records built from them.

Observe does not edit these files. `tests/test_otel_contract.py` sends the points and logs through the same path as the agent (protobuf encoding, decode, normalize, ingest) and checks the host view, the pushed host check, the threshold rules and the boot classification. When the agent's mapping changes, copy the new files here, update the source commit above, and let the contract test show what Observe must follow.
