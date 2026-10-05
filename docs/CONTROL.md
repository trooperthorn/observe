# Control actions

Status: design, being built. watchpost can ask a host to:

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

- **Rate limits:** at most one pending command per host per action, and at most 10 commands per host per hour. Reboot is limited to once per 15 minutes.
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
