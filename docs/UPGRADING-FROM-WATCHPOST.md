# Upgrading from watchpost to Observe

The product formerly called watchpost is now Observe. The old names still work for now and each use logs one warning, so an upgrade can be done in two steps: pull and restart first, then rename files at your own pace. Nothing is ever moved or copied for you.

## What changed

| Old name | New name |
| --- | --- |
| `WATCHPOST_*` environment variables | `OBSERVE_*` |
| `/config/watchpost.yaml` | `/config/observe.yaml` |
| `/data/watchpost.db` | `/data/observe.db` |
| Metrics `watchpost_*` | `observe_*` |
| MQTT topic prefix `watchpost/` | `observe/` |
| Session cookie `watchpost_session` | `observe_session` (you sign in once more) |
| Compose service, container and image `watchpost` | `observe` |
| `python -m watchpost` | `python -m observe` |

The key markers `wpi_`, `wpc_` and `wpf_` are opaque markers already issued to hosts and phones. They are unchanged, and so is the `watchpost_public_key` setting in a host's `control.toml`, which the host daemon reads.

## Commands for the Raspberry Pi at ~/observe

Run these in the checkout. If your checkout is still in `~/watchpost`, rename that folder to `~/observe` first.

```bash
cd ~/observe
docker compose down
git pull
mv config/watchpost.yaml config/observe.yaml
mv data/watchpost.db data/observe.db
mv secrets/watchpost_control_key secrets/observe_control_key   # only if the control key has that name
sed -i 's#watchpost_control_key#observe_control_key#' config/observe.yaml
sed -i 's/WATCHPOST_/OBSERVE_/g' .env config/observe.yaml
docker compose build
docker compose up -d
docker compose logs observe | head -20
```

Check three things after the edit:

1. In `config/observe.yaml`, `plugin_settings.control.signing_key_file` points at the renamed key file, for example `/run/secrets/observe_control_key`.
2. `server.db_path`, if you set it, reads `/data/observe.db`.
3. Alert targets of type `mqtt` that relied on the default `topic_prefix` now publish under `observe/`. Set `topic_prefix: watchpost` on the target if Home Assistant or another subscriber still listens on the old topics.

Update Prometheus rules, Grafana panels and any scrape job that use `watchpost_` metric names to the `observe_` names. Remove the old image with `docker image rm watchpost:local` once the new container is healthy.

## What the warnings mean

- `environment variable WATCHPOST_X is the old name; set OBSERVE_X instead`: the value was read from the old variable. Rename it in `.env`. If both are set, the new one wins and there is no warning.
- `using /config/watchpost.yaml, the old name`: no `/config/observe.yaml` exists, so the old file was used. Rename it as above.
- `using /data/watchpost.db, the old name`: no `/data/observe.db` exists, so the old database was used and your history is intact. Stop the container, rename the file, and start it again. Starting without the old file would create a new empty database.
- A warning about the entry point group `watchpost.plugins`: an installed third-party plugin still registers under the old group. Ask its author to publish under `observe.plugins`.
