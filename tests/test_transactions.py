"""db.transaction() inside another transaction is a savepoint of it, so several
model writes can be made all or nothing (strata-admin import applies a file so)."""

from typing import Any

import pytest


def _keys(app: Any) -> set[str]:
    from strata.db import get_db

    rows = get_db().execute("SELECT key FROM app_setting WHERE key LIKE 't.%'").fetchall()
    return {str(r[0]) for r in rows}


def _put(cur: Any, key: str) -> None:
    cur.execute("INSERT INTO app_setting (key, value) VALUES (?, '1')", (key,))


def test_a_failed_inner_transaction_undoes_only_its_own_writes(app: Any) -> None:
    from strata.db import transaction

    with app.app_context():
        with transaction() as cur:
            _put(cur, "t.outer")
            with pytest.raises(RuntimeError), transaction() as inner:
                _put(inner, "t.inner")
                raise RuntimeError
            with transaction() as inner:
                _put(inner, "t.kept")
        assert _keys(app) == {"t.outer", "t.kept"}


def test_a_failed_outer_transaction_undoes_its_inner_ones(app: Any) -> None:
    from strata.db import get_db, transaction

    with app.app_context():
        with pytest.raises(RuntimeError), transaction() as cur:
            _put(cur, "t.outer")
            with transaction() as inner:
                _put(inner, "t.inner")
            raise RuntimeError
        assert _keys(app) == set()
        assert not get_db().in_transaction
