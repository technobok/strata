"""Per-build and per-query resource limits, and the build timeout."""

import json

from tests.conftest import Strata

SETTINGS_SCRIPT = (
    "CREATE TABLE s AS SELECT current_setting('memory_limit') AS mem, "
    "current_setting('threads') AS threads"
)


def _first_row(strata: Strata, dm_uuid: str, body: str, macro: str) -> dict:
    rpt = strata.new_report(dm_uuid, body=body, name=macro, macro=macro)
    resp = strata.client.get(f"/r/{rpt}.json")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return json.loads(resp.get_data(as_text=True))[0]


def test_builds_run_with_the_configured_limits(strata: Strata) -> None:
    strata.set_config({"build.memory_limit": "3GB", "build.threads": 1})
    dm_uuid = strata.new_datamodule(script=SETTINGS_SCRIPT)
    strata.build(dm_uuid)
    row = _first_row(strata, dm_uuid, "SELECT * FROM s", "s")
    assert row == {"mem": "2.7 GiB", "threads": 1}  # 3GB = 2.79 GiB, as DuckDB prints it


def test_a_build_script_may_raise_its_own_limit(strata: Strata) -> None:
    dm_uuid = strata.new_datamodule(script="SET memory_limit = '5GB';\n" + SETTINGS_SCRIPT)
    strata.build(dm_uuid)
    assert _first_row(strata, dm_uuid, "SELECT * FROM s", "s")["mem"] == "4.6 GiB"


def test_report_queries_run_with_the_serve_limit(strata: Strata) -> None:
    strata.set_config({"serve.memory_limit": "1GB"})
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    row = _first_row(strata, dm_uuid, "SELECT current_setting('memory_limit') AS mem", "m")
    assert row == {"mem": "953.6 MiB"}


def test_a_bad_memory_setting_fails_the_build_clearly(strata: Strata) -> None:
    strata.set_config({"build.memory_limit": "lots"})
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert "build.memory_limit" in (dm.last_build_error or "")


def test_a_build_over_its_time_limit_is_interrupted(strata: Strata) -> None:
    strata.set_config({"build.timeout_seconds": 1})
    dm_uuid = strata.new_datamodule(
        script="CREATE TABLE t AS SELECT count(*) AS n FROM range(100000000000) r(x) WHERE x % 7 = 3"
    )
    strata.build(dm_uuid)
    dm = strata.datamodule(dm_uuid)
    assert dm.build_status == "failed"
    assert "time limit of 1s" in (dm.last_build_error or "")


def _duckdb_default_memory() -> str:
    import duckdb

    conn = duckdb.connect()
    try:
        return str(conn.execute("SELECT current_setting('memory_limit')").fetchone()[0])  # type: ignore[index]
    finally:
        conn.close()


def test_the_duckdb_default_box_overrides_the_build_limit(strata: Strata) -> None:
    strata.set_config({"build.memory_limit": "3GB", "build.memory_duckdb_default": True})
    dm_uuid = strata.new_datamodule(script=SETTINGS_SCRIPT)
    strata.build(dm_uuid)
    assert _first_row(strata, dm_uuid, "SELECT * FROM s", "s")["mem"] == _duckdb_default_memory()


def test_the_duckdb_default_box_overrides_the_serve_limit(strata: Strata) -> None:
    strata.set_config({"serve.memory_limit": "1GB", "serve.memory_duckdb_default": True})
    dm_uuid = strata.new_datamodule()
    strata.build(dm_uuid)
    row = _first_row(strata, dm_uuid, "SELECT current_setting('memory_limit') AS mem", "m")
    assert row == {"mem": _duckdb_default_memory()}


def test_a_blank_limit_means_the_default_from_the_shell_too(strata: Strata) -> None:
    from click.testing import CliRunner

    from strata import config
    from strata.cli import main
    from strata.db import transaction

    runner = CliRunner()
    assert runner.invoke(main, ["config", "set", "build.memory_limit", "3GB"]).exit_code == 0
    result = runner.invoke(main, ["config", "set", "build.memory_limit", ""])
    assert result.exit_code == 0, result.output
    assert "build.memory_limit cleared: the default (4GB) applies" in result.output
    with strata.app.app_context():
        config.forget_cached_settings()
        assert config.live_str("build.memory_limit") == "4GB"
        # A blank stored before blank meant "default" everywhere reads as unset too.
        with transaction() as cur:
            cur.execute(
                "INSERT INTO app_setting (key, value, description) VALUES ('build.memory_limit', '', '')"
            )
        config.forget_cached_settings()
        assert config.live_str("build.memory_limit") == "4GB"
