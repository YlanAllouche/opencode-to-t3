"""Reusable archive importer CLI; staging is the default."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile

from .assets import Assets
from t3_archive_core.publication import publish_assets
from .common import ImportFailure, backup, connection, now, require
from .converter import convert
from .references import resolve
from .render import alternate_id, build, refresh
from .source import Source
from .target import check_contract, choose, clone_snapshot, finish, load_bundle, merge_provenance, stamp_digest, store


def default_t3():
    return Path.home() / ".t3/userdata"


def environment_id(root):
    file = Path(root) / "environment-id"
    return file.read_text().strip() if file.exists() else None


def path_map(value):
    old, sep, new = value.partition("=")
    if not sep or not old or not new or not Path(new).expanduser().is_absolute():
        raise argparse.ArgumentTypeError("Expected OLD_ABSOLUTE_PATH=NEW_ABSOLUTE_PATH")
    return old.rstrip("/"), str(Path(new).expanduser().resolve())


def parser():
    p = argparse.ArgumentParser(prog="opencode-t3", description="Copy OpenCode 1.x/2.x conversations into inert T3 archives.")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("list", "inspect", "import"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--input", type=Path, help="OpenCode SQLite database or its containing directory (default: XDG data/opencode/opencode.db)")
        if name != "list":
            cmd.add_argument("--session", action="append", help="Select a source session; repeatable. Import default: all")
            cmd.add_argument("--no-related", action="store_true", help="Exclude ancestor, fork and child closure")
    p_import = sub.choices["import"]
    p_import.add_argument("--t3-data", type=Path, default=default_t3(), help="Existing T3 userdata directory")
    p_import.add_argument("--project", required=True, help="Existing target T3 project ID")
    p_import.add_argument("--output", type=Path, help="New staged userdata directory (default: timestamped local directory)")
    p_import.add_argument("--work-dir", type=Path, help="New private work/report directory (default: OS temporary directory)")
    mode = p_import.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Back up and update --t3-data in place")
    mode.add_argument("--dry-run", action="store_true", help="Validate in an isolated SQLite copy; leave destination unchanged")
    p_import.add_argument("--duplicates", choices=("skip", "prefer-newer", "keep-both"), default="skip")
    p_import.add_argument("--opencode2", type=Path, help="OpenCode 2.0.27 executable; required for v1 input")
    p_import.add_argument("--source-name", default=socket.gethostname(), help="Source machine label preserved as provenance")
    p_import.add_argument("--path-map", action="append", default=[], type=path_map, help="OLD=NEW prefix mapping for attachment files; repeatable")
    p_import.add_argument("--unarchived", action="store_true", help="Import settled but unarchived (default: archived and settled)")
    p_projects = sub.add_parser("projects", help="List target T3 project IDs")
    p_projects.add_argument("--t3-data", type=Path, default=default_t3())
    p_resolve = sub.add_parser("resolve", help="Resolve original IDs using only T3 data")
    p_resolve.add_argument("reference")
    p_resolve.add_argument("--t3-data", type=Path, default=default_t3())
    p_resolve.add_argument("--environment-id")
    return p


def lineage_for(sid, snapshots, mapping, existing_ids):
    session = snapshots[sid]["session"]
    parent = session.get("fork_session_id") or session.get("parent_id")
    relation = "fork" if session.get("fork_session_id") else "subagent" if parent else None
    if parent not in mapping and parent not in existing_ids:
        parent, relation = None, None
    root, seen = sid, set()
    cursor = parent
    while cursor in snapshots:
        require(cursor not in seen and cursor != sid, "Cycle in source session lineage")
        seen.add(cursor)
        root = cursor
        row = snapshots[cursor]["session"]
        cursor = row.get("fork_session_id") or row.get("parent_id")
    return dict(parentThreadId=mapping.get(parent, parent), relationshipToParent=relation,
                rootThreadId=mapping.get(root, root))




def run_import(args):
    require(not (args.apply and args.output), "--output stages a copy; omit it with --apply")
    require(not (args.dry_run and args.output), "--dry-run uses its work directory; omit --output")
    root = args.t3_data.expanduser().resolve()
    database = root / "statev2.sqlite"
    require(database.is_file(), "Existing T3 statev2.sqlite not found: " + str(database))
    with connection(database, readonly=True) as db:
        check_contract(db)
        require(db.execute("SELECT 1 FROM projection_projects WHERE project_id=? AND deleted_at IS NULL", (args.project,)).fetchone(), "Unknown T3 project; use opencode-t3 projects")
    if args.work_dir:
        work = args.work_dir.expanduser().resolve()
        require(not work.is_relative_to(root), "Work directory must be outside T3 userdata")
        work.mkdir(parents=True, exist_ok=False)
    else:
        work = Path(tempfile.mkdtemp(prefix="opencode-t3-"))
    report = dict(format="opencode-t3-import-report-v1", startedAt=now(), workDirectory=str(work),
                  mode="apply" if args.apply else "dry-run" if args.dry_run else "stage", duplicatePolicy=args.duplicates,
                  adapter="t3-0.1.7", sourceName=args.source_name, results=[], sourceOpenedReadOnly=True, targetCommitted=False)
    try:
        with Source(args.input) as source:
            require(source.path != database.resolve(), "OpenCode input and T3 target must be different databases")
            selected = source.select(args.session, not args.no_related)
            require(selected, "No source sessions selected")
            snapshots = {sid: source.snapshot(sid) for sid in selected}
            report.update(source=str(source.path), sourceFormat=source.format, selectedSessions=selected)
            existing = {}
            with connection(database, readonly=True) as db:
                db.execute("BEGIN")
                existing_ids = {r[0] for r in db.execute("SELECT thread_id FROM orchestration_v2_projection_threads WHERE deleted_at IS NULL")}
                for sid in selected:
                    existing[sid] = load_bundle(db, sid)
            todo = [sid for sid in selected if not (args.duplicates == "skip" and existing[sid])]
            converted = convert(source, todo, work / "conversion", args.opencode2) if todo and source.format == "v1" else {sid: snapshots[sid] for sid in todo}
            assets = Assets(work / "assets", args.path_map)
            provenance = dict(sourceName=args.source_name, sourcePath=str(source.path), sourceFormat=source.format, importedAt=report["startedAt"])
            provisional, decisions, mapping = {}, {}, {sid: sid for sid in selected}
            for sid in selected:
                if sid not in todo:
                    decisions[sid] = "skip"
                    continue
                b = build(snapshots[sid], converted[sid], source.format, args.project, sid, assets, provenance, archived=not args.unarchived)
                stamp_digest(b)
                provisional[sid] = b
                decision = choose(existing[sid], b, args.duplicates)
                decisions[sid] = decision
                if decision == "alternate":
                    mapping[sid] = alternate_id(sid, b["archive"]["sourceFingerprint"])
            # Rebuild with final IDs so children of alternate snapshots point to the alternate parent.
            lineage_snapshots = {sid: {**s, "session": {**s["session"], **converted.get(sid, {}).get("session", {})}} for sid, s in snapshots.items()}
            bundles = {}
            for sid in todo:
                if decisions[sid] in ("skip", "older", "identical"):
                    continue
                bundles[sid] = build(snapshots[sid], converted[sid], source.format, args.project, mapping[sid], assets, provenance,
                                     archived=not args.unarchived, lineage=lineage_for(sid, lineage_snapshots, mapping, existing_ids))
                stamp_digest(bundles[sid])
            # Delete provisional assets that are no longer referenced after remapping.
            used = {a["relativePath"] for b in bundles.values() for a in b["archive"]["assets"]}
            for file in assets.root.iterdir():
                if file.name not in used:
                    file.unlink()
            for sid in selected:
                session = lineage_snapshots[sid]["session"]
                report["results"].append(dict(sourceId=sid, threadId=mapping[sid], result=decisions[sid],
                    cwd=session.get("directory"), parentId=session.get("parent_id"), forkSessionId=session.get("fork_session_id"),
                    unresolvedRelations=[p for p in (session.get("parent_id"), session.get("fork_session_id")) if p and p not in mapping and p not in existing_ids],
                    warnings=bundles[sid]["archive"]["warnings"] if sid in bundles else []))
            # Close source snapshot before destination writes. Original connections are read-only.
        if args.apply:
            backup(database, work / "t3-before.sqlite")
            report["backup"] = str(work / "t3-before.sqlite")
            target, destination_assets = database, root / "attachments"
        elif args.dry_run:
            target, destination_assets = work / "validation.sqlite", work / "validated-attachments"
            backup(database, target)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output = (args.output or Path.cwd() / ("opencode-import-" + stamp)).expanduser().resolve()
            require(not output.is_relative_to(root), "Output directory must be outside source T3 userdata")
            require(not output.exists(), "Staged output must be a new directory: " + str(output))
            output.mkdir(parents=True)
            target, destination_assets = output / "statev2.sqlite", output / "attachments"
            backup(database, target)
            if (root / "attachments").exists():
                shutil.copytree(root / "attachments", destination_assets)
            if (root / "environment-id").exists():
                shutil.copy2(root / "environment-id", output / "environment-id")
        created_files = []
        try:
            with connection(target) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                check_contract(db)
                before_sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM orchestration_events").fetchone()[0]
                for sid in selected:
                    current = load_bundle(db, sid)
                    if sid not in provisional:
                        # Skip still needs to be true if another process removed the archive.
                        require(current, "Destination changed during planning; rerun import")
                        continue
                    decision = choose(current, provisional[sid], args.duplicates)
                    require(decision == decisions[sid], "Destination changed during planning; rerun import")
                    if sid not in bundles:
                        if decision == "identical":
                            merge_provenance(db, current, provenance)
                        continue
                    bundle = bundles[sid]
                    if decision == "alternate":
                        alternate = load_bundle(db, bundle["thread"]["id"])
                        if alternate:
                            require(alternate["archive"] and alternate["archive"].get("sourceFingerprint") == bundle["archive"]["sourceFingerprint"], "Deterministic snapshot ID collision")
                            next(r for r in report["results"] if r["sourceId"] == sid)["result"] = "identical-snapshot"
                            merge_provenance(db, alternate, provenance)
                            continue
                    if decision == "replace":
                        saved = clone_snapshot(current, assets, root / "attachments")
                        old_copy = load_bundle(db, saved["thread"]["id"])
                        if not old_copy:
                            store(db, saved)
                        else:
                            require(old_copy["archive"] and old_copy["archive"].get("sourceFingerprint") == current["archive"]["sourceFingerprint"], "Previous snapshot ID collision")
                        # Preserve organization choices when updating the canonical archive.
                        for key in ("archivedAt", "settledOverride", "settledAt", "unsettledAt", "pinnedAt", "pinOrderKey", "snoozedAt", "snoozedUntil"):
                            bundle["thread"][key] = current["thread"].get(key)
                        refresh(bundle)
                        stamp_digest(bundle)
                        next(r for r in report["results"] if r["sourceId"] == sid)["previousSnapshotId"] = saved["thread"]["id"]
                    store(db, bundle, replace=decision == "replace")
                created_files = publish_assets(assets.root, destination_assets)
                after_sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM orchestration_events").fetchone()[0]
                if after_sequence > before_sequence:
                    finish(db)
                report["addedEvents"] = after_sequence - before_sequence
            report.update(target=str(target), attachments=str(destination_assets), completedAt=now(),
                          targetCommitted=not args.dry_run, validationPassed=True, addedAttachmentFiles=len(created_files))
        except BaseException:
            for file in created_files:
                file.unlink(missing_ok=True)
            raise
        env = environment_id(root)
        for r in report["results"]:
            if env:
                r["link"] = f"<t3-thread://v2/{env}/{r['threadId']}>"
        (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    except BaseException as exc:
        report.update(error=str(exc), failedAt=now())
        (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        raise ImportFailure(str(exc) + "\nReport: " + str(work / "report.json")) from exc


def main(argv=None):
    os.umask(0o077)
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "import":
            result = run_import(args)
        elif args.command in ("list", "inspect"):
            with Source(args.input) as source:
                if args.command == "list":
                    result = dict(source=str(source.path), format=source.format, sessions=source.sessions())
                else:
                    ids = source.select(args.session, not args.no_related)
                    result = dict(source=str(source.path), format=source.format,
                                  sessions=[dict(session=s["session"], messages=len(s["message"] if source.format == "v1" else s["session_message"]),
                                                 parts=len(s["part"])) for s in (source.snapshot(sid) for sid in ids)])
        elif args.command == "projects":
            with connection(args.t3_data / "statev2.sqlite", readonly=True) as db:
                result = dict(projects=[dict(r) for r in db.execute("SELECT project_id,title,workspace_root FROM projection_projects WHERE deleted_at IS NULL")])
        else:
            result = dict(reference=args.reference, matches=resolve(args.t3_data / "statev2.sqlite", args.reference,
                args.environment_id or environment_id(args.t3_data)))
        print(json.dumps(result, indent=2))
        return 0
    except (ImportFailure, OSError, sqlite3.Error, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
