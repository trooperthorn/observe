# Control actions

Status: the Observe side is built (the `control` plugin). The hostwatch-control daemon and the thermalctl overrides file are built in their own repos. Observe can ask a host to:

- change a fan floor;
- switch a fan controller between dry run and active;
- restart an allowlisted service;
- reboot;
- update the hostwatch agent container, and (off by default) the control daemon itself.

Each host's own allowlist has the final say. These owner decisions were made on 2026-10-04:

- hosts pull commands;
- a separate control daemon runs on each host;
- the host's allowlist is the authority;
- all four actions are in the first build. `agent.update` was added on 2026-10-10 with the
  Updates page (README "Updating").

## Parts

| Part | Repo | Role |
|---|---|---|
| `observe-control` plugin | ipMontior, `plugins/control` | Action catalogue, confirmation UI, signing, the command queue, results and audit |
| `hostwatch-control` daemon | hostwatch, `hostwatch/control/` | Runs on each host under its own service and account, separate from the read-only collector. Pulls, verifies, checks the local allowlist, executes and reports |
| thermalctl overrides | thermal-control-linux | A root-owned overrides file that thermalctl validates and merges, so nothing rewrites the main config |
| Thermal Control Suite | thermal-control-suite | Its existing Administrator-level IPC, called by the daemon running as LocalSystem |

## Flow

1. **An admin requests an action in Observe.**
   - Every action needs a confirmation step.
   - A reboot also needs the admin to type the host name exactly.
   - The request is audited with who, what and the parameters.
2. **Observe writes a command and signs it with its Ed25519 control key.** The private key file lives under `/run/secrets` and is never served or logged. The command looks like this:

   ```json
   {"v":1,"id":"uuid","host":"MediaIn-SVR","action":"fan.set_floor",
    "params":{"controller":"thermalctl","header":"pwm2","min_duty":20},
    "requested_by":"sean","issued_at":1759600000,"expires_at":1759600120,"seq":42}
   ```

   The signature covers the canonical JSON: sorted keys, no spaces, UTF-8.
3. **The daemon on the host pulls the command.** It polls `GET /api/v1/control/commands?host=` every 5 seconds over its outbound connection, using a control key (`wpc_`) bound to that host. The pull returns only commands for that host that have not expired.
4. **The daemon checks the command before acting.** Each step must pass:
   - the signature, against the Observe public key pinned in its root-owned config;
   - that `host` equals its own name;
   - that the command has not expired, allowing 30 seconds of clock skew;
   - that `id` has not been seen before and `seq` is higher than the last one executed, which it persists;
   - that the action and its parameters are allowed by its local allowlist.

   Any failure is refused, and the refusal is reported and logged locally.
5. **The daemon executes the action and reports the result.** It sends the outcome, output (truncated and redacted) and timings to `POST /api/v1/control/results`. Both sides audit it.

Observe never connects to a host, and no host opens a listening port. A compromised Observe can only request actions that are in a host's own allowlist, and only within that allowlist's limits.

## Signing and keys

This section is a contract shared with the hostwatch-control daemon. Change it only in both repos together.

- **Canonical JSON:** the command object serialised with keys sorted at every level, separators `,` and `:` with no spaces, and UTF-8 with non-ASCII characters written as themselves, not as unicode escapes. NaN and infinity are not allowed.
- **Signature:** Ed25519 over those bytes, sent as standard base64 (with padding) of the 64 raw bytes. A pulled command is `{"command": {...}, "signature": "<base64>"}`, and the daemon verifies the signature over its own canonical form of `command`, never over the received text.
- **Public key:** `ed25519:<standard base64 of the 32 raw bytes>`, pinned in each host's `control.toml` as `observe_public_key` (hosts installed before the rename may still use the old key name, which the host daemon also reads).
- **Private key:** an unencrypted PKCS8 PEM file at `plugin_settings.control.signing_key_file` (default `/run/secrets/observe_control_key`). On POSIX the plugin refuses to start when the file is readable by group or others, and also when it is missing or is not an Ed25519 key. It is never served, logged or audited.
- **Creating a key:** `python -m observe --control-keygen PATH` writes a new key at PATH with mode 0600, refuses to overwrite an existing file, and prints only the public key.
- **Control keys:** the Add host flow (`POST /api/hosts`, docs/ARCHITECTURE.md "Host enrolment") mints the `wpc_` key when the host redeems its single-use install token, so an enrolled host needs no manual key step, and the allowlist chosen there (fans with an optional `min_duty_limit`, services, reboot) is what the generated `control.toml` is built from. On Linux and Raspberry Pi the install script writes `/etc/hostwatch/control.toml` (root-owned, mode 0640, group `hostwatch-control`) with `observe_public_key`, `host`, the local `machine_id` and the allowlist, writes `control.env` the same way, renders the sudoers file with hostwatch's `render_sudoers`, checks it with `visudo -c`, and starts the unit. `control.toml` has one `min_duty_floor` for the whole fan table, so the script uses the largest `min_duty_limit` chosen for any header. Control cannot be enrolled for Windows yet, and the TrueNAS script installs the agent only, so the Add host flow refuses control there as well (the script URL answers 409). The console wizard (`/hosts/new`) greys out the control choice for both platforms and states the reason in text, and asks for the lowest remote duty per fan header, which becomes the `min_duty_limit` thermalctl needs. The saved allowlist can be changed later on the host settings page (`/hosts/{name}/settings`): Observe stores the new list and makes a short update command that rewrites `control.toml` and the sudoers rules on the host and restarts the daemon, with the same guards as the install script. The page shows Pending until the update has run, Written once `control.toml` holds the list, and Applied after the daemon next pulls with its key. The host keeps enforcing its own file, so nothing changes on the host until the update command is run there. A host's daemon pulls with a `wpc_` key bound to its host name. Admins create and revoke these in the key screen or with `--ingest-key-create HOST --ingest-key-scope wpc` and `--ingest-key-revoke ID`. A `wpc_` key is refused by host ingest and by field reports, and `wpi_` and `wpf_` keys are refused by the control routes. A `wpc_` key may pull only for its own host: another `host` value gets 403.
- **Settings** (`plugin_settings.control`): `signing_key_file`, `pull_interval_s` (the polling interval the daemon is advised to use, default 5), `max_pending_per_action` (1), `max_commands_per_host_per_hour` (10), `reboot_min_interval_s` (900) and `command_ttl_s` (how long a new command stays valid, default 120).

## Queue, pull and results

- **Tables:** `control_commands` holds id, host, action, params (canonical JSON text), requested_by, issued_at, expires_at, seq, state and signature. `seq` starts at 1 for each host and goes up by one, and is unique per host. `control_results` holds one row per reported result: command id, host, state, redacted output, whether it was truncated, started and finished times, duration, received time and the key prefix. Both are created by the plugin's own migrations.
- **States:** `requested` (written), `pulled` (first handed to the daemon), `scheduled` (the daemon accepted a delayed action such as a reboot), and the final states `done`, `failed`, `refused`, `cancelled` and `unknown`.
- **Pull:** `GET /api/v1/control/commands?host=` with a `wpc_` key returns `{"host": ..., "commands": [{"command": {...}, "signature": "..."}], "cancel": ["<command id>", ...]}`. `commands` is in seq order and holds only that host's `requested` or `pulled` commands that have not expired. A `scheduled` command has already been answered, so it is not served again and does not block a command of another action. `cancel` lists this host's commands that an admin cancelled while they were `scheduled`, until the host posts a `cancelled` result for it (at most seven days), so an offline daemon still learns of the cancel; handling an id twice must be harmless. For such a command the host posts `cancelled` when it stopped the reboot, or `done` or `failed` when the cancel arrived too late, so Observe records what really happened. The first delivery of a command is audited as `control_pull`; an empty poll writes nothing.
- **Results:** `POST /api/v1/control/results` takes `{"id", "state", "output", "started_at", "finished_at"}`, where state is `done`, `failed`, `refused` or `scheduled`, output is optional text and the times are optional seconds. A result is accepted only for a command this host has pulled: a command still `requested` gets 409. `scheduled` is accepted only for `host.reboot` (422 otherwise) and only from `pulled`. Output is stripped of control characters, redacted (`wpi_`, `wpc_`, `wpf_` and `hw_` keys, `Bearer` values, `Authorization` header lines, PEM blocks, values after `password`, `token`, `secret` or `api_key`, and base64 or hex runs of 32 or more characters) and cut to 4096 characters before it is stored. The route answers 404 for an unknown id and for another host's id, with the same body, so ids cannot be probed, and 409 for a command that is already final or has expired. Every refusal is audited with the reason. A `scheduled` command may receive a later result such as `done`.
- **Expiry:** a `requested` or `pulled` command whose `expires_at` has passed with no result becomes `unknown` (audited as `control_expired`), and a late result is refused. It is never shown as done, because Observe cannot tell whether the host acted. Admins read recent commands, with their state and latest result, at `GET /api/v2/control/commands`.
- **Rate limits** are checked when a command is written, in the same transaction that assigns `seq`, so concurrent requests cannot pass them together: at most `max_pending_per_action` open commands per host and action (an open command is requested, pulled or scheduled), at most `max_commands_per_host_per_hour` per host, and one `host.reboot` per `reboot_min_interval_s` per host. A refusal is audited as `control_request_refused`, and a written command as `control_requested` with who, what and the parameters.

## Admin requests and history

These routes need an admin session, and every POST needs the CSRF token (`X-CSRF-Token`). A non-admin session gets 403 and a missing or wrong token gets 403.

- **Request:** `POST /api/plugins/control/request` takes `{"host", "action", "params", "confirmed", "confirm_host"}`. The host must have reported at least once (404 otherwise). `confirmed` must be the JSON boolean `true`, which is what the dialog's Confirm button sends; the strings "true" and "yes" and the number 1 are refused (400 otherwise). A header or service name must match its pattern in full, so a trailing newline or space is refused before signing (422). For `host.reboot`, `confirm_host` must equal `host` character for character (400 otherwise). A success returns `{"id", "state": "requested", "seq", "expires_at"}` and goes through the queue, so the rate limits (429) and signing apply. Every refusal is audited as `control_request_refused` with the reason.
- **Parameters:** each action takes exactly these parameters and nothing else (422 otherwise). `fan.set_floor`: `controller` (`thermalctl` or `thermal-control-suite`), `header` (1 to 32 letters, digits, dashes or underscores, not starting with a dash, the rule hostwatch-control applies; dots are refused) and `min_duty` (a whole number from 0 to 100). `fan.set_mode`: `controller` and `mode` (`dry_run` or `active`). `service.restart`: `name` (1 to 128 letters, digits and `. _ @ : -`). `host.reboot`: none. `agent.update`: `component` (`agent`, `control` or `all`).
- **Capabilities:** Observe compares a request with what it knows of the host. Control is available only when the host has an unrevoked `wpc_` key that its daemon has pulled with; otherwise every request is refused (422) and the host page shows no Control card. The controller must be the host's own (`thermal-control-suite` on Windows, `thermalctl` elsewhere). For a host added with control chosen, the saved allowlist is checked as the generated `control.toml` would check it: a header must be listed and `min_duty` must be at least the file's `min_duty_floor` (the largest `min_duty_limit`), a service must be listed, a reboot or an update must be allowed, `fan.set_mode` is refused (the file says `allow_mode_change = false`) and an update may name only `agent`. `agent.update` is offered only to a Linux or Raspberry Pi agent new enough to run it. When thermalctl has reported fans, a thermalctl `header` must be one of them, and `fan.set_mode` for thermalctl needs a thermalctl source. `GET /api/v2/control/capabilities?host=` returns whether control is `available` (and the `reason` when not), the allowed actions, the host's controller, modes, the components an update may name, the saved allowlist and the `fan_headers` with each header's floor, and the reported headers, and the page builds every choice from it.
- **Header names:** thermalctl names a fan header by the PWM channel that drives it, and reports it as `hw.id` `fan:pwm1` to `fan:pwmN`. The Add host wizard offers board names, `fan1` to `fan3`; `fanN` is `pwmN`. Observe maps the two in one place (`observe.enrol.controller_id`): the generated `control.toml` lists the controller ids, and a `fan.set_floor` for `fan1` is signed for `pwm1`. A host installed before this mapping still lists `fan1` in its file and refuses `pwm1` until the settings update command is run again.
- **Update all:** `POST /api/plugins/control/update-agents` takes `{"component", "confirmed": true}` and queues one `agent.update` per eligible host: a host that has pushed, whose platform is `linux` or `raspberry-pi`, that has an active `wpc_` key and whose daemon has pulled at least once (`GET /api/v2/updates/agents` lists the hosts with that verdict and the reason). Each command goes through the queue above, so the per-host rate limits and `max_pending_per_action` apply to each host on its own. The answer is `{"component", "queued": [{"host", "id"}], "refused": [{"host", "reason"}]}`; a host refused by a rate limit or by eligibility is listed, never silently skipped. The Updates page (`/admin/updates`) calls it from its Update all button after a confirm dialog.
- **Cancel:** `POST /api/plugins/control/commands/{id}/cancel` changes a command in state `requested` (not yet pulled) or `scheduled` to `cancelled` with one guarded update, for any action. A `pulled`, final or expired command gets 409, and an unknown id gets 404. Both are audited (`control_cancelled`, `control_cancel_refused`). A cancelled command is final, so a later result is refused with 409, and it is no longer served. A command cancelled while `scheduled` appears in the `cancel` list of the next pulls, which is how the daemon learns to cancel the delayed reboot.
- **History:** `GET /api/v2/control/commands?host=` lists that host's commands, newest first, with state and latest result. The Control section of the host page (`/host?name=`) shows it for admins as the last card of the host page, offers the four actions, asks for confirmation in the shared `<dialog>` (the reboot is a separate danger button that needs the host name typed), shows states as status chips, shows a refusal as an inline notice plus an error toast, and shows a Cancel reboot button while a reboot is scheduled, and shows an `agent.update` result as the old and new version read from the daemon's output. All text on the page is written with `textContent`.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/v1/control/commands?host=` | `wpc_` key for that host | The daemon pulls its pending signed commands |
| `POST /api/v1/control/results` | `wpc_` key for that host | The daemon reports an outcome |
| `POST /api/plugins/control/request` | admin session and CSRF | Request an action, with confirmation |
| `POST /api/plugins/control/commands/{id}/cancel` | admin session and CSRF | Cancel a requested or scheduled command |
| `POST /api/plugins/control/update-agents` | admin session and CSRF | Queue `agent.update` for every eligible host |
| `GET /api/v2/control/commands?host=` | admin session | Command history with state and result |
| `GET /api/v2/control/capabilities?host=` | admin session | Whether control is available, valid actions, allowlist headers with floors, services |
| `GET /api/v2/control/commands`, `GET /api/v2/control/capabilities?host=` | admin session | The same two reads on the v2 API. The list never writes: a command that expired without an answer is shown as `unknown` from the clock, and the legacy list also stores that state |

## Setup

The normal route is the Add host wizard at `/hosts/new` (steps 1 and 2 below once, then the wizard for each host). Steps 3 to 5 are the manual route for a host that cannot use the wizard, and they are what the install script does for you.

1. Install the plugin into the Observe image (`pip install ./plugins/control`) and list it as `plugins: [control]` in the Observe config.
2. Create the signing key: `python -m observe --control-keygen /run/secrets/observe_control_key`. The command prints only the public key. Keep the file readable by the Observe user alone.
3. Put the printed `ed25519:...` public key in each host's `control.toml` as `observe_public_key`, together with that host's allowlist.
4. Create one control key per host: `python -m observe --config CONFIG --ingest-key-create HOST --ingest-key-scope wpc`. The `wpc_` key is shown once. Store it in the daemon's root-owned config on that host.
5. Start the hostwatch-control daemon on the host. It pulls every 5 seconds and refuses anything its own allowlist does not permit.
6. To rotate the signing key, run keygen with a new path, update `observe_public_key` on every host, then point `signing_key_file` at the new file. To revoke a host key, use `--ingest-key-revoke ID`.

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
observe_public_key = "ed25519:..."
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

[update]
agent = true                       # agent.update may replace the hostwatch-agent container
control = false                    # agent.update may upgrade and restart this daemon
```

A missing `[update]` table means no update is allowed, like a missing `[reboot]`. Observe's
install and update commands write `[update]` with `agent` set from the "Allow agent updates from
Observe" choice (on by default in the Add host wizard and on the host settings page) and
`control = false`, because a control self-update restarts the daemon that is running the
command. Turn `control` on by hand on a host where that is wanted.

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

Parameters: none. The host name the admin typed (`confirm_host`) is checked by Observe in the admin request body and is never part of the signed command or its params.

- The daemon schedules the reboot after `delay_s` and reports `scheduled`.
- Observe shows a cancel button for that window. The host can also cancel locally with `hostwatch-control cancel`.

### `agent.update`

Parameter: component (`agent`, `control` or `all`). Added with the Updates page
(`/admin/updates`, README "Updating"), which offers it per host and for every eligible host at
once. The console asks for confirmation in the shared dialog like `service.restart`.

This is a contract shared with the hostwatch-control daemon (hostwatch repository,
`hostwatch/control/`). Change it only in both repositories together.

- **Allowlist:** the `[update]` table above. `agent` must be true for component `agent`, `control`
  must be true for component `control`, and `all` needs both; otherwise the daemon refuses with the
  reason, like any other action its allowlist does not permit.
- **Component `agent`:** the daemon pulls the image the agent was installed with, which is
  `ghcr.io/trooperthorn/hostwatch:edge` unless the running container was started from another
  tag (it reads the tag from the running `hostwatch-agent` container, so a host pinned to a
  release stays on that release line). It then recreates `hostwatch-agent` the way the installer
  does (`observe/scripts.py`, `install_agent`): the running container is stopped and renamed
  `hostwatch-agent-prev`, the new one is started with the installer's arguments (`--network host`,
  `--user 10001:10001`, `--read-only`, `--tmpfs /tmp`, `--cap-drop ALL`,
  `--security-opt no-new-privileges:true`, `--env-file /etc/hostwatch/agent.env`, the `/sys`,
  journal and thermalctl read-only mounts and the `hostwatch-agent-data` volume), and only when
  the new container is running is `hostwatch-agent-prev` removed. If the new container does not
  start, the previous one is renamed back and started again, and the result is `failed`. The
  `agent.env` file is read by Docker, never by the daemon, and no key is ever in the output.
- **Component `control`:** the daemon runs `pip install --upgrade` of `hostwatch[control]` in
  `/opt/hostwatch-control/venv` from the source the installer used, posts the result, and only
  then schedules its own restart through systemd (`systemctl restart hostwatch-control` through
  the same sudo rule set as `service.restart`, started detached so the result reaches Observe
  first). `all` does the agent first and the daemon second, so a failed agent update leaves the
  daemon as it was.
- **Result:** `done` with the output a JSON object
  `{"old_image_id": "sha256:...", "new_image_id": "sha256:...", "old_version": "1.4.0",
  "new_version": "1.5.0"}`. For component `control` the image ids are empty strings and the
  versions are the package versions before and after; for `all` the agent's values are
  reported. When the image was already current the ids and versions are equal and the state is
  still `done`. A failure is `failed` with the reason as plain text. Observe shows the result in
  the Control card of the host page and on the Updates page as "old to new", or "already at
  new" when nothing changed.
- **Rate limits:** the usual ones (one pending per host and action, ten commands per host per
  hour). The Updates page's Update all applies them per host and lists the hosts it could not
  queue.

## Limits and safety

- **Rate limits:** at most one pending command per host per action, and at most 10 commands per host per hour. Reboot is limited to once per 15 minutes. These are enforced when the command is written (see Queue, pull and results).
- **Results:** a command with no result after its expiry is shown as `unknown`, never as done.
- **Health checks after an action:**
  - After a fan change, Observe shows the controller status from the next agent batch.
  - After a reboot, it watches for the host's new boot through the existing boot classifier.
- **Audit:** every request, sign, pull, refusal, result and cancel is audited, both in Observe and in the host's log.

## Threat model additions

- **Stolen Observe control key:** it is bounded by each host's allowlist, and it can be rotated by changing the pinned key on the hosts.
- **Stolen host control key:** it can only pull that host's commands and post results. It cannot create commands.
- **Replay:** each command id is used once, `seq` must increase, and commands expire.
- **A malicious admin session:** CSRF, per-action confirmation, the typed host name for reboot, rate limits and the audit log.
- **Plain HTTP on the LAN:** signatures still protect integrity. Confidentiality relies on the LAN, and TLS is recommended.
