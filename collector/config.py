"""Settings dataclass + environment loader (stdlib only)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # OneAPI (own read-only client)
    zs_vanity_domain: str
    zs_client_id: str
    zs_client_secret: str
    zpa_customer_id: str
    # receiver
    lss_port: int = 4639
    lss_cert_path: str = "/data/receiver.crt"
    lss_key_path: str = "/data/receiver.key"
    dedup_ttl_seconds: int = 300
    max_line_bytes: int = 65536
    # classification defaults (UI-overridable)
    warm_days: int = 7
    stale_days: int = 30
    window_days: int = 30
    # serving
    http_port: int = 8866
    dash_token: str = ""
    # engine
    flush_seconds: int = 30
    db_path: str = "/data/state.db"
    config_sync_hour: int = 2          # nightly hour (UTC) for auto sync
    # privacy / sampling
    user_sample_enabled: bool = True
    recent_users_max: int = 50
    # scoping / advisory
    donotretire_list: tuple[str, ...] = ()
    connector_group_filter: tuple[str, ...] = ()
    health_webhook: str | None = None


_REQUIRED = ("ZS_VANITY_DOMAIN", "ZS_CLIENT_ID", "ZS_CLIENT_SECRET",
             "ZPA_CUSTOMER_ID", "DASH_TOKEN")


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    v = raw.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    raise SystemExit(f"Config error: expected 'true' or 'false', got {raw!r}")


def _parse_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Config error: {key} must be an integer, got {raw!r}")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from env (os.environ when None). SystemExit on missing required vars."""
    if env is None:
        env = dict(os.environ)

    missing = [k for k in _REQUIRED if not (env.get(k) or "").strip()]
    if missing:
        raise SystemExit(
            "Config error: required environment variable(s) not set: "
            + ", ".join(missing)
        )

    return Settings(
        zs_vanity_domain=env["ZS_VANITY_DOMAIN"],
        zs_client_id=env["ZS_CLIENT_ID"],
        zs_client_secret=env["ZS_CLIENT_SECRET"],
        zpa_customer_id=env["ZPA_CUSTOMER_ID"],
        lss_port=_parse_int(env, "LSS_PORT", 4639),
        lss_cert_path=env.get("LSS_CERT_PATH") or "/data/receiver.crt",
        lss_key_path=env.get("LSS_KEY_PATH") or "/data/receiver.key",
        dedup_ttl_seconds=max(1, _parse_int(env, "DEDUP_TTL_SECONDS", 300)),
        max_line_bytes=_parse_int(env, "MAX_LINE_BYTES", 65536),
        warm_days=_parse_int(env, "WARM_DAYS", 7),
        stale_days=_parse_int(env, "STALE_DAYS", 30),
        window_days=_parse_int(env, "WINDOW_DAYS", 30),
        http_port=_parse_int(env, "HTTP_PORT", 8866),
        dash_token=env["DASH_TOKEN"],
        flush_seconds=_parse_int(env, "FLUSH_SECONDS", 30),
        db_path=env.get("DB_PATH") or "/data/state.db",
        config_sync_hour=_parse_int(env, "CONFIG_SYNC_HOUR", 2),
        user_sample_enabled=_parse_bool(env.get("USER_SAMPLE_ENABLED"), default=True),
        recent_users_max=_parse_int(env, "RECENT_USERS_MAX", 50),
        donotretire_list=_parse_list(env.get("DONOTRETIRE_LIST")),
        connector_group_filter=_parse_list(env.get("CONNECTOR_GROUP_FILTER")),
        health_webhook=(env.get("HEALTH_WEBHOOK") or None),
    )
