"""Live settings: a change saved in /admin/config applies without a restart.

``Applies.LIVE`` settings are read from app_setting at use (cached 5 s per
process; a write in this process drops the cache). Everything else is loaded
into app.config at create_app and says which restart it needs.
"""

import apsw
import pytest

from strata import config
from strata.config import ENV_OVERRIDES, KEY_MAP, REGISTRY, Applies
from tests.conftest import Strata
from tests.test_limits import SETTINGS_SCRIPT, _first_row


def test_a_saved_build_setting_applies_to_the_next_build(strata: Strata) -> None:
    strata.set_config({"build.memory_limit": "3GB"})
    dm_uuid = strata.new_datamodule(script=SETTINGS_SCRIPT)
    strata.build(dm_uuid)
    assert _first_row(strata, dm_uuid, "SELECT mem FROM s", "s1") == {"mem": "2.7 GiB"}

    # Same app, no create_app in between: the next build reads the new value.
    strata.set_config({"build.memory_limit": "1GB"})
    assert strata.flashes()[-1] == (
        "success",
        "Configuration saved. Applies immediately: build.memory_limit.",
    )
    strata.build(dm_uuid)
    assert _first_row(strata, dm_uuid, "SELECT mem FROM s", "s2") == {"mem": "953.6 MiB"}


def test_a_change_from_another_process_is_seen_once_the_cache_expires(
    strata: Strata, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(config, "_now", lambda: clock[0])
    with strata.app.app_context():
        assert config.live_str("build.memory_limit") == "4GB"  # the default; now cached

        # The worker's view of a change made elsewhere (another web process, or
        # `strata-admin config set`): a write this process did not make.
        other = apsw.Connection(strata.app.config["DATABASE_PATH"])
        other.execute(
            "INSERT INTO app_setting (key, value, description) VALUES ('build.memory_limit', '1GB', '')"
        )
        other.close()
        assert config.live_str("build.memory_limit") == "4GB"  # still within the cache

        clock[0] += config.LIVE_CACHE_SECONDS
        assert config.live_str("build.memory_limit") == "1GB"


def test_live_settings_have_no_app_config_copy() -> None:
    live = [e.key for e in REGISTRY if e.applies is Applies.LIVE]
    assert "build.memory_limit" in live and "worker.poll_interval" in live
    # A copy in app.config would be read once at create_app and go stale.
    assert [k for k in live if k in KEY_MAP] == []
    assert [k for k in live if k in ENV_OVERRIDES] == []


def test_only_live_settings_are_read_live(strata: Strata) -> None:
    with strata.app.app_context():
        with pytest.raises(ValueError, match="not a live setting"):
            config.live_int("server.port")
        with pytest.raises(TypeError, match="int setting"):
            config.live_str("build.threads")


def test_the_config_page_says_when_each_change_applies(strata: Strata) -> None:
    page = strata.client.get("/admin/config").get_data(as_text=True)
    assert "Takes effect" in page
    for label in ("applies immediately", "restart the app", "restart the worker"):
        assert label in page

    strata.set_config({"server.port": 5099, "build.threads": 2})
    assert strata.flashes()[-1] == (
        "success",
        "Configuration saved. Applies immediately: build.threads. Restart the app: server.port.",
    )
    strata.set_config({"server.port": 5099})
    assert strata.flashes()[-1] == ("success", "Configuration saved (no changes).")


def test_a_read_racing_a_write_does_not_cache_the_old_value(
    strata: Strata, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strata import db as strata_db
    from strata.models.app_setting import set_setting

    real_get_db = strata_db.get_db

    class WriteLandsMidRead:
        """Runs the read, then another thread's write commits before it is cached."""

        def execute(self, sql: str) -> WriteLandsMidRead:
            self.rows = real_get_db().execute(sql).fetchall()
            monkeypatch.setattr(strata_db, "get_db", real_get_db)
            set_setting("build.memory_limit", "1GB")
            return self

        def fetchall(self) -> list:
            return self.rows

    with strata.app.app_context():
        monkeypatch.setattr(strata_db, "get_db", lambda: WriteLandsMidRead())
        assert config.live_str("build.memory_limit") == "4GB"  # read before the write
        assert config.live_str("build.memory_limit") == "1GB"  # not the cached old read
