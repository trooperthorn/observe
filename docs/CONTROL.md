# Control actions

Status: design, being built. Built so far: the plugin skeleton, the signing key and `wpc_` keys (see Signing and keys), and the command queue with expiry, seq, rate limits, the pull route and the results route (see Queue, pull and results). The action catalogue, confirmation UI and cancel are not built yet, so nothing creates commands except code calling the queue. watchpost can ask a host to:

- change a fan floor;
- switch a fan controller between dry run and active;
- restart an allowlisted service;
- reboot.

Each host's own allowlist has the final say. These owner decisions were made on 2026-10-04:

- hosts pull commands;
- a separate control daemon runs on each host;
- the host's allowlist is the authority;
- all four actions are in the first build.

## Parts

| Part | Repo | Role |
|---|---|---|
| `watchpost-control` plugin | ipMontior, `plugins/control` | Action catalogue, confirmation UI, signing, the command queue, results and audit |
| `hostwatch-control` daemon | hostwatch, `hostwatch/control/` | Runs on each host under its own service and account, separate from the read-only collector. Pulls, verifies, checks the local allowlist, executes and reports |
| thermalctl overrides | thermal-control-linux | A root-owned overrides file that thermalctl validates and merges, so nothing rewrites the main config |
| Thermal Control Suite | thermal-control-suite | Its existing Administrator-level IPC, called by the daemon running as LocalSystem |

## Flow

1. **An admin requests an action in watchpost.**
   - Every action needs a confirmation step.
   - A reboot also needs the admin to type the host name exactly.
   - The request is audited with who, what and the parameters.
2. **watchpost writes a command and signs it with its Ed25519 control key.** The private key file lives under `/run/secrets` and is never served or logged. The command looks like this:

   ```json
   {"v":1,"id":"uuid","host":"MediaIn-SVR","action":"fan.set_floor",
    "params":{"controller":"thermalctl","header":"pwm2","min_duty":20},
    "requested_by":"sean","issued_at":1759600000,"expires_at":1759600120,"seq":42}
   ```

   The signature covers the canonical JSON: sorted keys, no spaces, UTF-8.
3. **The daemon on the host pulls the command.** It polls `GET /api/v1/control/commands?host=` every 5 seconds over its outbound connection, using a control key (`wpc_`) bound to that host. The pull returns only commands for that host that have not expired.
4. **The daemon checks the command before acting.** Each step must pass:
   - the signature, against the watchpost public key pinned in its root-owned config;
   - that `host` equals its own name;
   - that the command has not expired, allowing 30 seconds of clock skew;
   - that `id` has not been seen before and `seq` is higher than the last one executed, which it persists;
   - that the action and its parameters are allowed by its local allowlist.

   Any failure is refused, and the refusal is reported and logged locally.
5. **The daemon executes the action and reports the result.** It sends the outcome, output (truncated and redacted) and timings to `POST /api/v1/control/results`. Both sides audit it.

watchpost never connects to a host, and no host opens a listening port. A compromised watchpost can only request actions that are in a host's own allowlist, and only within that allowlist's limits.

## Signing and keys

This section is a contract shared with the hostwatch-control daemon. Change it only in both repos together.

- **Canonical JSON:** the command object serialised with keys sorted at every level, separators `,` and `:` with no spaces, and UTF-8 with non-ASCII characters written as themselves, not as unicode escapes. NaN and infinity are not allowed.
- **Signature:** Ed25519 over those bytes, sent as standard base64 (with padding) of the 64 raw bytes. A pulled command is `{"command": {...}, "signature": "<base64>"}`, and the daemon verifies the signature over its own canonical form of `command`, never over the received text.
- **Public key:** `ed25519:<standard base64 of the 32 raw bytes>`, pinned in each host's `control.toml` as `watchpost_public_key`.
- **Private key:** an unencrypted PKCS8 PEM file at `plugin_settings.control.signing_key_file` (default `/run/secrets/watchpost_control_key`). On POSIX the plugin refuses to start when the file is readable by group or others, and also when it is missing or is not an Ed25519 key. It is never served, logged or audited.
- **Creating a key:** `python -m watchpost --control-keygen PATH` writes a new key at PATH with mode 0600, refuses to overwrite an existing file, and prints only the public key.
- **Control keys:** a host's daemon pulls with a `wpc_` key bound to its host name. Admins create and revoke these in the key screen or with `--ingest-key-create HOST --ingest-key-scope wpc` and `--ingest-key-revoke ID`. A `wpc_` key is refused by host ingest and by field reports, and `wpi_` and `wpf_` keys are refused by the control routes. A `wpc_` key may pull only for its own host: another `host` value gets 403.
- **Settings** (`plugin_settings.control`): `signing_key_file`, `pull_interval_s` (the polling interval the daemon is advised to use, default 5), `max_pending_per_action` (1), `max_commands_per_host_per_hour` (10), `reboot_min_interval_s` (900) and `command_ttl_s` (how long a new command stays valid, default 120).

## Queue, pull and results

- **Tables:** `control_commands` holds id, host, action, params (canonical JSON text), requested_by, issued_at, expires_at, seq, state and signature. `seq` starts at 1 for each host and goes up by one, and is unique per host. `control_results` holds one row per reported result: command id, host, state, redacted output, whether it was truncated, started and finished times, duration, received time and the key prefix. Both are created by the plugin's own migrations.
- **States:** `requested` (written), `pulled` (first handed to the daemon), `scheduled` (the daemon accepted a delayed action such as a reboot), and the final states `done`, `failed`, `refused`, `cancelled` and `unknown`.
- **Pull:** `GET /api/v1/control/commands?host=` with a `wpc_` key returns `{"host": ..., "commands": [{"command": {...}, "signature": "..."}]}` in seq order. It holds only that host's commands that are not final and have not expired, plus any `scheduled` command, which has already been answered. The first delivery of a command is audited as `control_pull`; an empty poll writes nothing.
- **Results:** `POST /api/v1/control/results` takes `{"id", "state", "output", "started_at", "finished_at"}`, where state is `done`, `failed`, `refused` or `scheduled`, output is optional text and the times are optional seconds. Output is stripped of control characters, redacted (key-shaped text, `Bearer` values, and values after `password`, `token`, `secret` or `api_key`) and cut to 4096 characters before it is stored. The route answers 404 for an unknown id and for another host's id, with the same body, so ids cannot be probed, and 409 for a command that is already final or has expired. Every refusal is audited with the reason. A `scheduled` command may receive a later result such as `done`.
- **Expiry:** a `requested` or `pulled` command whose `expires_at` has passed with no result becomes `unknown` (audited as `control_expired`), and a late result is refused. It is never shown as done, because watchpost cannot tell whether the host acted. Admins read recent commands, with their state and latest result, at `GET /api/plugins/control/commands`.
- **Rate limits** are checked when a command is written, in the same transaction that assigns `seq`, so concurrent requests cannot pass them together: at most `max_pending_per_action` open commands per host and action (an open command is requested, pulled or scheduled), at most `max_commands_per_host_per_hour` per host, and one `host.reboot` per `reboot_min_interval_s` per host. A refusal is audited as `control_request_refused`, and a written command as `control_requested` with who, what and the parameters.

### Test vector

Both sides must reproduce these values. The key pair comes from the 32-byte seed `00 01 02 ... 1f`.

- Private seed, base64: `AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=`
- Public key: `ed25519:A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=`
- Command:

  ```json
  {"v":1,"id":"6f1c2a52-8d0e-4c53-9a53-0d1f6d2f7a10","host":"MediaIn-SVR","action":"fan.set_floor","params":{"controller":"thermalctl","header":"pwm2","min_duty":20},"requested_by":"sean","issued_at":1759600000,"expires_at":1759600120,"seq":42}
  ```

- Canonical JSON (the bytes that are signed):

  ```json
  {"action":"fan.set_floor","expires_at":1759600120,"host":"MediaIn-SVR","id":"6f1c2a52-8d0e-4c53-9a53-0d1f6d2f7a10","issued_at":1759600000,"params":{"controller":"thermalctl","header":"pwm2","min_duty":20},"requested_by":"sean","seq":42,"v":1}
  ```

- Signature, base64: `ctLtz6sxgqiI2PRdESeNINEzokkV7Nq+X60xM++KglMxeMpVoqW7/xqXGQtRxXNu/p0aSkfjNSdzIAvoqA+ICg==`

Changing any field of the command must make verification fail.

## Local allowlist

The allowlist is `/etc/hostwatch/control.toml` on Linux and `C:\ProgramData\hostwatch\control.toml` on Windows. Only root, or SYSTEM and Administrators, can write it.

```toml
watchpost_public_key = "ed25519:..."
host = "MediaIn-SVR"

[fan]
controller = "thermalctl"          # or "thermal-control-suite"
headers = ["pwm1", "pwm2", "pwm3", "pwm4"]
min_duty_floor = 20                # requests below this are refused
min_duty_ceiling = 100
allow_mode_change = true

[services]
restart = ["hostwatch-agent", "nut-monitor", "docker:scrutiny"]

[reboot]
allow = true
delay_s = 60                       # cancellable during the delay
```

## Actions

### `fan.set_floor`

Parameters: controller, header, min_duty.

- **thermalctl:**
  - The daemon writes `/etc/thermalctl/overrides.toml` atomically.
  - It runs `thermalctl check-config` on the merged result, then reloads thermalctl.
  - thermalctl keeps its own hard limits: the measured stall floors and full speed on any fault.
  - If the check fails, the old overrides are restored.
- **Thermal Control Suite:** the daemon calls its Administrator-level IPC with the same limits.

### `fan.set_mode`

Parameters: controller, mode (`dry_run` or `active`).

- For thermalctl this needs a restart, because a reload refuses to change the mode. The daemon writes the override, checks the config and restarts the service.
- Switching to active requires every header in the override to be marked mapped.

### `service.restart`

Parameter: name. The name must appear in the local `restart` list.

- **Linux:** `systemctl restart <unit>`, run through a sudo rule limited to the listed units. `docker:<name>` runs `docker restart`.
- **Windows:** `Restart-Service` against a fixed name. The name is never interpolated into a script.

### `host.reboot`

Parameters: none, apart from the host name the admin typed on the watchpost side.

- The daemon schedules the reboot after `delay_s` and reports `scheduled`.
- watchpost shows a cancel button for that window. The host can also cancel locally with `hostwatch-control cancel`.

## Limits and safety

- **Rate limits:** at most one pending command per host per action, and at most 10 commands per host per hour. Reboot is limited to once per 15 minutes. These are enforced when the command is written (see Queue, pull and results).
- **Results:** a command with no result after its expiry is shown as `unknown`, never as done.
- **Health checks after an action:**
  - After a fan change, watchpost shows the controller status from the next agent batch.
  - After a reboot, it watches for the host's new boot through the existing boot classifier.
- **Audit:** every request, sign, pull, refusal, result and cancel is audited, both in watchpost and in the host's log.

## Threat model additions

- **Stolen watchpost control key:** it is bounded by each host's allowlist, and it can be rotated by changing the pinned key on the hosts.
- **Stolen host control key:** it can only pull that host's commands and post results. It cannot create commands.
- **Replay:** each command id is used once, `seq` must increase, and commands expire.
- **A malicious admin session:** CSRF, per-action confirmation, the typed host name for reboot, rate limits and the audit log.
- **Plain HTTP on the LAN:** signatures still protect integrity. Confidentiality relies on the LAN, and TLS is recommended.
