"""The serve grammar's pure parts: partitioning params and validating fields."""

import pytest

from strata.services.query_filter import FilterError, build_clauses, parse_params

COLUMNS = [("region", "VARCHAR"), ("revenue", "DOUBLE"), ("net sales", "DOUBLE")]


def test_declared_arg_wins_over_filter() -> None:
    args, spec = parse_params([("region", "North"), ("revenue__gte", "100")], {"region"})
    assert args == {"region": ["North"]}
    assert [(f.key, f.value) for f in spec.filters] == [("revenue__gte", "100")]


def test_reserved_keys_are_partitioned() -> None:
    args, spec = parse_params(
        [("q", "nor"), ("q", "th"), ("sort", "-revenue,region"), ("page", "2"), ("version", "v1")],
        set(),
    )
    assert args == {}
    assert spec.q == ["nor", "th"]
    assert spec.sort_raw == ["-revenue", "region"]
    assert spec.page == 2
    assert spec.version == "v1"


@pytest.mark.parametrize("page", ["0", "-1", "two"])
def test_bad_page_is_rejected(page: str) -> None:
    with pytest.raises(FilterError):
        parse_params([("page", page)], set())


def test_unknown_field_is_rejected() -> None:
    _, spec = parse_params([("colour__eq", "red")], set())
    with pytest.raises(FilterError):
        build_clauses(spec, COLUMNS)


def test_unknown_op_is_rejected() -> None:
    _, spec = parse_params([("revenue__like", "1")], set())
    with pytest.raises(FilterError):
        build_clauses(spec, COLUMNS)


def test_underscore_matches_a_spaced_column() -> None:
    _, spec = parse_params([("net_sales__gt", "5")], set())
    clauses = build_clauses(spec, COLUMNS)
    assert '"net sales"' in clauses.where_sql


def test_values_are_bound_never_interpolated() -> None:
    payload = "x' OR 1=1 --"
    _, spec = parse_params([("region", payload)], set())
    clauses = build_clauses(spec, COLUMNS)
    assert payload not in clauses.where_sql
    assert payload in clauses.binds
