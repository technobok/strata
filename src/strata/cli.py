"""CLI entry point for strata-admin."""

import sys
from datetime import UTC, datetime

import click

from strata.config import (
    LIVE_CACHE_SECONDS,
    REGISTRY,
    Applies,
    parse_value,
    resolve_entry,
    serialize_value,
)
from strata.db import (
    close_standalone_db,
    get_db_path,
    get_standalone_db,
    init_db_at,
    standalone_transaction,
)

# ---------------------------------------------------------------------------
# Config helpers (standalone DB, no Flask)
# ---------------------------------------------------------------------------


def _db_get(key: str) -> str | None:
    """Read a single value from app_setting."""
    db = get_standalone_db()
    row = db.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def _db_get_all() -> dict[str, str]:
    """Read all app_setting rows into a dict."""
    db = get_standalone_db()
    rows = db.execute("SELECT key, value FROM app_setting ORDER BY key").fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _db_set(key: str, value: str) -> None:
    """Upsert a value into app_setting."""
    with standalone_transaction() as cursor:
        cursor.execute(
            "INSERT INTO app_setting (key, value, description) VALUES (?, ?, '') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def main() -> None:
    """Strata administration tool."""


# ---- config group --------------------------------------------------------


@main.group()
def config() -> None:
    """View and manage configuration settings."""


@config.command("list")
def config_list() -> None:
    """Show all settings with their effective values."""
    db_values = _db_get_all()

    current_group = ""
    for entry in REGISTRY:
        group = entry.key.split(".")[0]
        if group != current_group:
            if current_group:
                click.echo()
            click.echo(click.style(f"[{group}]", bold=True))
            current_group = group

        raw = db_values.get(entry.key)
        if raw is not None:
            value = raw
            source = "db"
        else:
            value = serialize_value(entry, entry.default)
            source = "default"

        if entry.secret and raw is not None:
            display = "********"
        else:
            display = value if value else "(empty)"

        source_tag = click.style(f"[{source}]", fg="cyan" if source == "db" else "yellow")
        click.echo(f"  {entry.key} = {display}  {source_tag}")
        click.echo(click.style(f"    {entry.description}", dim=True))

    close_standalone_db()


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Get the effective value of a setting."""
    entry = resolve_entry(key)
    if not entry:
        click.echo(f"Unknown setting: {key}", err=True)
        sys.exit(1)
    assert entry is not None

    raw = _db_get(key)
    if raw is not None:
        value = parse_value(entry, raw)
    else:
        value = entry.default

    if entry.secret and raw is not None:
        click.echo("********")
    elif isinstance(value, list):
        click.echo(", ".join(value) if value else "(empty)")
    elif isinstance(value, bool):
        click.echo("true" if value else "false")
    else:
        click.echo(value if value else "(empty)")

    close_standalone_db()


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value in the database."""
    entry = resolve_entry(key)
    if not entry:
        click.echo(f"Unknown setting: {key}", err=True)
        sys.exit(1)
    assert entry is not None

    try:
        parse_value(entry, value)
    except (ValueError, TypeError) as exc:
        click.echo(f"Invalid value for {key} ({entry.type.value}): {exc}", err=True)
        sys.exit(1)

    _db_set(key, value)
    # A running app/worker re-reads a live setting once its cache expires.
    applies = (
        f"applies within {LIVE_CACHE_SECONDS:g} s"
        if entry.applies is Applies.LIVE
        else entry.applies.value
    )
    click.echo(f"{key} = {value} ({applies})")
    close_standalone_db()


@config.command("export")
@click.argument("output_file", type=click.Path())
def config_export(output_file: str) -> None:
    """Export all settings as a shell script of make config-set calls."""
    db_values = _db_get_all()
    lines = [
        "#!/bin/bash",
        "# Configuration export for Strata",
        f"# Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
    ]

    for entry in REGISTRY:
        raw = db_values.get(entry.key)
        if raw is not None:
            value = raw
            lines.append(f"make config-set KEY={entry.key} VAL='{value}'")
        else:
            value = serialize_value(entry, entry.default)
            lines.append(f"# default: {entry.key}")
            lines.append(f"# make config-set KEY={entry.key} VAL='{value}'")

    import os
    import stat

    with open(output_file, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(output_file, os.stat(output_file).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    click.echo(f"Exported {len(REGISTRY)} settings to {output_file}")
    close_standalone_db()


# ---- admin commands ------------------------------------------------------


@main.command("init-db")
def init_db_command() -> None:
    """Initialize the database schema."""
    db_path = get_db_path()
    init_db_at(db_path)
    click.echo("Database initialized.")


@main.command("reconcile")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be pruned/removed without changing anything.",
)
def reconcile_command(dry_run: bool) -> None:
    """Enforce version retention and sweep orphaned datamodule files."""
    from strata import create_app
    from strata.services import datamodule_service

    app = create_app()
    with app.app_context():
        rep = datamodule_service.reconcile(dry_run=dry_run)

    verb = "would prune" if dry_run else "pruned"
    for line in rep.prune_lines:
        click.echo(f"{verb} version {line}")
    verb = "would remove" if dry_run else "removed"
    for path in rep.orphan_dirs:
        click.echo(f"{verb} orphan datamodule dir {path}")
    for path in rep.stale_tmp:
        click.echo(f"{verb} stale tmp file {path}")
    for path in rep.orphan_files:
        click.echo(f"{verb} orphan version file {path}")
    for path in rep.stale_scratch:
        click.echo(f"{verb} stale export/spill entry {path}")
    for line in rep.expired_runs:
        click.echo(f"{verb} expired run {line}")
    for path in rep.orphan_snapshots:
        click.echo(f"{verb} orphan run snapshot {path}")
    for line in rep.stuck_builds:
        click.echo(f"{'would mark' if dry_run else 'marked'} stuck build failed: {line}")
    for warning in rep.warnings:
        click.echo(f"WARNING: {warning}")
    click.echo(
        f"reconcile: {rep.rows_deleted} version row(s), {rep.files_deleted} file(s), "
        f"{rep.bytes_freed} bytes; {len(rep.orphan_dirs)} orphan dir(s), "
        f"{len(rep.stale_tmp)} stale tmp, {len(rep.orphan_files)} orphan file(s), "
        f"{len(rep.stale_scratch)} stale export/spill, {len(rep.stuck_builds)} stuck build(s), "
        f"{len(rep.expired_runs)} expired run(s), {len(rep.orphan_snapshots)} orphan snapshot(s), "
        f"{len(rep.warnings)} warning(s)" + (" (dry-run: no changes made)" if dry_run else "")
    )
