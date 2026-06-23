import pytest

import collector
from collector.config import Settings, load_settings


BASE_ENV = {
    "ZS_VANITY_DOMAIN": "acme",
    "ZS_CLIENT_ID": "cid",
    "ZS_CLIENT_SECRET": "csec",
    "ZPA_CUSTOMER_ID": "999",
    "DASH_TOKEN": "tok",
}


def test_import_collector_works():
    assert collector.__version__


def test_settings_fixture_discovered(settings):
    assert isinstance(settings, Settings)
    assert settings.zs_vanity_domain == "acme"


def test_load_settings_minimal_required():
    s = load_settings(dict(BASE_ENV))
    assert s.zs_vanity_domain == "acme"
    assert s.zs_client_id == "cid"
    assert s.zs_client_secret == "csec"
    assert s.zpa_customer_id == "999"
    assert s.dash_token == "tok"


@pytest.mark.parametrize("missing", sorted(BASE_ENV))
def test_load_settings_missing_required_raises_systemexit(missing):
    env = dict(BASE_ENV)
    del env[missing]
    with pytest.raises(SystemExit) as ei:
        load_settings(env)
    assert missing in str(ei.value)


def test_load_settings_int_defaults():
    s = load_settings(dict(BASE_ENV))
    assert s.lss_port == 4639
    assert s.http_port == 8866
    assert s.flush_seconds == 30
    assert s.warm_days == 7
    assert s.stale_days == 30
    assert s.window_days == 30
    assert s.dedup_ttl_seconds == 300
    assert s.config_sync_hour == 2
    assert s.recent_users_max == 50


def test_load_settings_int_overrides():
    env = dict(BASE_ENV, LSS_PORT="5000", HTTP_PORT="9000", FLUSH_SECONDS="15",
               WARM_DAYS="3", STALE_DAYS="14", WINDOW_DAYS="90", RECENT_USERS_MAX="5")
    s = load_settings(env)
    assert s.lss_port == 5000
    assert s.http_port == 9000
    assert s.flush_seconds == 15
    assert s.warm_days == 3
    assert s.stale_days == 14
    assert s.window_days == 90
    assert s.recent_users_max == 5


def test_load_settings_comma_lists():
    env = dict(BASE_ENV, DONOTRETIRE_LIST="dr-app, audit-rule ,payroll",
               CONNECTOR_GROUP_FILTER="grpA,grpB")
    s = load_settings(env)
    assert s.donotretire_list == ("dr-app", "audit-rule", "payroll")
    assert s.connector_group_filter == ("grpA", "grpB")
    assert isinstance(s.donotretire_list, tuple)


def test_load_settings_empty_lists_default_to_empty_tuple():
    s = load_settings(dict(BASE_ENV))
    assert s.donotretire_list == ()
    assert s.connector_group_filter == ()


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False), ("FALSE", False),
])
def test_load_settings_bool_parsing(raw, expected):
    env = dict(BASE_ENV, USER_SAMPLE_ENABLED=raw)
    assert load_settings(env).user_sample_enabled is expected


def test_load_settings_bool_default_true():
    assert load_settings(dict(BASE_ENV)).user_sample_enabled is True


def test_load_settings_health_webhook_optional():
    assert load_settings(dict(BASE_ENV)).health_webhook is None
    env = dict(BASE_ENV, HEALTH_WEBHOOK="https://hook.example/x")
    assert load_settings(env).health_webhook == "https://hook.example/x"


def test_load_settings_bool_empty_string_uses_default():
    env = dict(BASE_ENV, USER_SAMPLE_ENABLED="")
    assert load_settings(env).user_sample_enabled is True


def test_load_settings_bool_invalid_raises_systemexit():
    env = dict(BASE_ENV, USER_SAMPLE_ENABLED="yes")
    with pytest.raises(SystemExit):
        load_settings(env)


def test_load_settings_whitespace_only_required_raises():
    env = dict(BASE_ENV, DASH_TOKEN="   ")
    with pytest.raises(SystemExit) as ei:
        load_settings(env)
    assert "DASH_TOKEN" in str(ei.value)


def test_load_settings_invalid_int_raises_systemexit():
    env = dict(BASE_ENV, LSS_PORT="4639.5")
    with pytest.raises(SystemExit) as ei:
        load_settings(env)
    assert "LSS_PORT" in str(ei.value)


def test_load_settings_dedup_ttl_floored_to_one():
    assert load_settings(dict(BASE_ENV, DEDUP_TTL_SECONDS="0")).dedup_ttl_seconds == 1
    assert load_settings(dict(BASE_ENV, DEDUP_TTL_SECONDS="-5")).dedup_ttl_seconds == 1
    assert load_settings(dict(BASE_ENV)).dedup_ttl_seconds == 300
