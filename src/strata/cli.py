"""CLI entry point for strata-admin."""

import fnmatch
import sys
from datetime import UTC, datetime
from pathlib import Path

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
    UnusableDatabase,
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


def _db_clear(key: str) -> None:
    """Delete a setting row so the registry default applies."""
    with standalone_transaction() as cursor:
        cursor.execute("DELETE FROM app_setting WHERE key = ?", (key,))


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
    """Set a configuration value in the database (a blank value clears it, as the
    /admin/config form does: the default applies again)."""
    entry = resolve_entry(key)
    if not entry:
        click.echo(f"Unknown setting: {key}", err=True)
        sys.exit(1)
    assert entry is not None

    # A running app/worker re-reads a live setting once its cache expires.
    applies = (
        f"applies within {LIVE_CACHE_SECONDS:g} s"
        if entry.applies is Applies.LIVE
        else entry.applies.value
    )
    if not value.strip():
        _db_clear(key)
        default = serialize_value(entry, entry.default)
        click.echo(f"{key} cleared: the default ({default or 'blank'}) applies ({applies})")
        close_standalone_db()
        return

    try:
        parse_value(entry, value)
    except (ValueError, TypeError) as exc:
        click.echo(f"Invalid value for {key} ({entry.type.value}): {exc}", err=True)
        sys.exit(1)

    _db_set(key, value)
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
    try:
        init_db_at(db_path)
    except UnusableDatabase as exc:
        raise click.ClickException(str(exc)) from exc
    # Every datamodule names its owners, viewers and alert list (2026-11-24): older
    # ones that followed the defaults get them written in, once. Never fails a start.
    try:
        from strata import create_app
        from strata.services import datamodule_service

        with create_app().app_context():
            frozen = datamodule_service.freeze_lists()
        if frozen:
            click.echo(f"Named the access and alert lists of {frozen} datamodule(s).")
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Could not name the datamodules' lists: {exc}", err=True)
    click.echo("Database initialized.")


@main.command("adopt-prepivot")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be kept, without changing anything.",
)
def adopt_prepivot_command(dry_run: bool) -> None:
    """Replace a database from the Strata before datamodules with a new one.

    The old file is kept beside it as a backup. The settings this Strata knows,
    the secret key and the connections are copied into the new database; the old
    reports, schedules and API links stay in the backup. Stop strata-web and
    strata-worker first.
    """
    from strata.prepivot import AdoptError, adopt

    try:
        result = adopt(get_db_path(), dry_run=dry_run)
    except AdoptError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.summary():
        click.echo(line)


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
        f"{rep.expired_reads} expired read-audit row(s), "
        f"{len(rep.warnings)} warning(s)" + (" (dry-run: no changes made)" if dry_run else "")
    )


# ---- definition files ------------------------------------------------------


@main.command("import")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Check every file and show what would change.")
@click.option(
    "--prune",
    is_flag=True,
    help="Also remove the reports, schedules, dependencies and connection bindings "
    "a file does not list.",
)
@click.option("--build", is_flag=True, help="Queue a build of each datamodule made or changed.")
@click.option("--only", default=None, help="Only datamodules whose path matches, e.g. 'checks/*'.")
@click.option(
    "--as",
    "actor",
    default="strata-admin",
    show_default=True,
    help="Recorded as who made or changed them.",
)
def import_command(
    paths: tuple[Path, ...], dry_run: bool, prune: bool, build: bool, only: str | None, actor: str
) -> None:
    """Make or update datamodules from definition files (TOML; a folder: every *.toml
    in it). Each file is all or nothing; see the help page 'Import and export'."""
    from strata import create_app
    from strata.models.datamodule import Datamodule
    from strata.services import build_queue, definitions

    defs, unreadable = definitions.read_files(paths)
    if only:
        defs = [d for d in defs if fnmatch.fnmatchcase(d.path, only)]
    with create_app().app_context():
        outcomes = definitions.import_definitions(defs, actor, dry_run=dry_run, prune=prune)
        if build and not dry_run:
            for o in outcomes:
                dm = Datamodule.get_by_uuid(o.datamodule_uuid) if o.datamodule_uuid else None
                if dm is not None and o.action in ("create", "update"):
                    build_queue.request_build(dm, f"manual:{actor}", force=True)

    for source, reason in unreadable:
        click.echo(f"REFUSED {source}: {reason}")
    for o in outcomes:
        if o.refused is not None:
            click.echo(f"REFUSED {o.path} ({o.source}): {o.refused}")
            continue
        changed = f": {', '.join(o.changes)}" if o.action == "update" else ""
        click.echo(f"{o.action} {o.path}{changed}")
        for note in o.notes:
            click.echo(f"  note: {note}")
    count = {a: sum(o.action == a for o in outcomes) for a in ("create", "update", "unchanged")}
    refused = len(unreadable) + sum(o.refused is not None for o in outcomes)
    click.echo(
        f"import: {count['create']} created, {count['update']} updated, "
        f"{count['unchanged']} unchanged, {refused} refused"
        + (" (dry run: nothing was changed)" if dry_run else "")
    )
    if refused:
        sys.exit(1)


@main.command("export")
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Folder to write into (made if missing); a datamodule a/b goes to a/b.toml.",
)
@click.option("--only", default=None, help="Only datamodules whose path matches, e.g. 'checks/*'.")
def export_command(out_dir: Path, only: str | None) -> None:
    """Write datamodules as definition files that import back unchanged."""
    from strata import create_app
    from strata.models.datamodule import Datamodule
    from strata.services import definitions

    written = 0
    with create_app().app_context():
        for dm in Datamodule.get_all():
            if only and not fnmatch.fnmatchcase(dm.path, only):
                continue
            target = definitions.export_path(out_dir, dm)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(definitions.export_datamodule(dm), encoding="utf-8")
            click.echo(f"wrote {target}")
            written += 1
    click.echo(f"export: {written} datamodule(s)")
