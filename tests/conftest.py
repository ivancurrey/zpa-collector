import pytest
from collector.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(zs_vanity_domain="acme", zs_client_id="id", zs_client_secret="sec",
                    zpa_customer_id="123", dash_token="t", db_path=str(tmp_path / "state.db"),
                    user_sample_enabled=True, recent_users_max=10)


@pytest.fixture
def db(settings):
    from collector import db as dbmod
    conn = dbmod.connect(settings.db_path)
    dbmod.init_schema(conn)
    yield conn
    conn.close()
