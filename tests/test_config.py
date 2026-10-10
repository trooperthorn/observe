import pytest

from observe.config import ConfigError, load_config

from .conftest import make_config


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


def test_env_and_file_refs(tmp_path, monkeypatch):
    secret = tmp_path / "comm"
    secret.write_text("from-file\n")
    monkeypatch.setenv("WP_PW", "from-env")
    # Forward slashes keep a Windows path free of backslashes, which a
    # double-quoted YAML scalar would read as escape sequences.
    cfg = load_config(_write(tmp_path, f"""
credentials:
  a: {{type: snmpv2c, community: "${{file:{secret.as_posix()}}}"}}
  b: {{type: mqtt, username: u, password: "${{WP_PW}}"}}
"""))
    assert cfg.credentials["a"].community == "from-file"
    assert cfg.credentials["b"].password == "from-env"


def test_unset_env_is_an_error_not_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("WP_MISSING", raising=False)
    with pytest.raises(ConfigError, match="WP_MISSING is not set"):
        load_config(_write(tmp_path, 'credentials: {a: {type: mqtt, username: u, password: "${WP_MISSING}"}}'))


def test_unknown_field_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Extra inputs"):
        load_config(_write(tmp_path, "monitors: [{name: x, type: tcp, host: h, port: 1, prot: 2}]"))


def test_credential_type_must_match_monitor():
    with pytest.raises(ValueError, match="expected"):
        make_config([{"name": "s", "type": "snmp", "host": "h", "credential": "win",
                      "mode": "uptime"}])


def test_disk_letter_is_constrained():
    with pytest.raises(ValueError):
        make_config([{"name": "d", "type": "winrm", "host": "h", "credential": "win",
                      "mode": "disk", "disk": "C:' OR 1=1"}])


def test_winrm_kerberos_requires_principal_and_keytab(tmp_path):
    with pytest.raises(ConfigError, match="kerberos transport needs principal and keytab_path"):
        load_config(_write(tmp_path, "credentials: {a: {type: winrm, transport: kerberos}}"))


def test_winrm_kerberos_accepts_principal_and_keytab(tmp_path):
    cfg = load_config(_write(tmp_path, """
credentials:
  a: {type: winrm, transport: kerberos, principal: svc@LAB.EXAMPLE.COM, keytab_path: /run/secrets/x.keytab}
"""))
    assert cfg.credentials["a"].principal == "svc@LAB.EXAMPLE.COM"
    assert cfg.credentials["a"].keytab_path == "/run/secrets/x.keytab"


def test_duplicate_slugs_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        make_config([{"name": "Core Switch", "type": "ping", "host": "a"},
                     {"name": "core-switch", "type": "ping", "host": "b"}])


def test_example_config_validates(monkeypatch):
    import pathlib
    for var in ("SNMP_COMMUNITY", "SNMP_AUTH", "SNMP_PRIV", "WINRM_PASSWORD",
                "MQTT_PASSWORD", "NTFY_TOKEN", "OBSERVE_UI_PASSWORD", "AD_PASSWORD", "TRUENAS_API_KEY", "PROXMOX_TOKEN_SECRET",
                "VSPHERE_PASSWORD", "HA_TOKEN", "UNIFI_API_KEY", "TECHNITIUM_TOKEN"):
        monkeypatch.setenv(var, "x")
    cfg = load_config(pathlib.Path(__file__).parent.parent / "config.example.yaml")
    assert {m.type for m in cfg.monitors} == {
        "ping", "tcp", "http", "dns", "tls_cert", "snmp", "winrm", "wmi", "mqtt", "linux",
        "docker", "truenas", "proxmox", "vsphere", "homeassistant", "unifi_network",
        "unifi_protect", "technitium", "pushed_host"}


@pytest.mark.parametrize("field", ["retention_days", "audit_retention_days"])
@pytest.mark.parametrize("value", [0, -1])
def test_retention_below_one_day_is_rejected(field, value):
    with pytest.raises(ValueError):
        make_config([{"name": "p", "type": "ping", "host": "h"}], server={field: value})


def test_a_monitor_target_with_an_example_placeholder_is_reported():
    """Bug plan follow-up: DNS monitors queried observe-svr.yourdomain from an example config."""
    from observe.config import placeholder_warnings
    from .conftest import make_config
    cfg = make_config([
        {"name": "DNS1 answers", "type": "dns", "nameserver": "192.0.2.53",
         "query": "observe-svr.yourdomain"},
        {"name": "Core", "type": "ping", "host": "10.0.0.1"}])
    lines = placeholder_warnings(cfg)
    assert len(lines) == 1 and "DNS1 answers" in lines[0] and "yourdomain" in lines[0]
