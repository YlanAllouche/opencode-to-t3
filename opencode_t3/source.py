"""Read consistent, credential-free session snapshots from OpenCode SQLite."""
import json
import os
from pathlib import Path

from .common import connection, require, rows, insert

SESSION_TABLES = ("message", "part", "todo", "session_message", "session_input", "session_context_epoch",
                  "session_inbox", "session_pending", "instruction_entry", "instruction_state")
RUNTIME_INPUTS = {"session_input", "session_inbox", "session_pending"}


def default_input():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "opencode/opencode.db"


def input_path(path=None):
    path = Path(path or default_input()).expanduser().resolve()
    if path.is_dir():
        path = path / "opencode.db"
    require(path.is_file(), "OpenCode database not found: " + str(path))
    return path


class Source:
    def __init__(self, path=None):
        self.path = input_path(path)
        self.context = connection(self.path, readonly=True)
        self.db = self.context.__enter__()
        try:
            self.db.execute("BEGIN")
            self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require("project" in self.tables, "Unsupported OpenCode database: project table missing")
            self.format = "v1"
            if "session_v2" in self.tables:
                state = self.db.execute("SELECT value FROM kv WHERE key='migration.v1-v2'").fetchone() if "kv" in self.tables else None
                if state and json.loads(state[0]) == {"phase": "completed"}:
                    self.format = "v2"
                else:
                    require(not self.db.execute("SELECT 1 FROM session_v2 LIMIT 1").fetchone(),
                            "OpenCode v2 migration is incomplete; finish it on an isolated copy")
            self.unknown_session_tables = []
            for table in self.tables - set(SESSION_TABLES) - {"session_share", "account", "credential", "control_account"}:
                quoted = table.replace('"', '""')
                if any(r[1] == "session_id" for r in self.db.execute(f'PRAGMA table_info("{quoted}")')):
                    self.unknown_session_tables.append(table)
            self.table = "session_v2" if self.format == "v2" else "session"
            require(self.table in self.tables, "Unsupported OpenCode session schema")
        except BaseException:
            self.context.__exit__(None, None, None)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.context.__exit__(*args)

    def sessions(self):
        return rows(self.db, f'SELECT * FROM "{self.table}" ORDER BY time_created,id')

    def select(self, ids=None, related=True):
        sessions = {s["id"]: s for s in self.sessions()}
        chosen = set(ids or sessions)
        require(chosen <= sessions.keys(), "Unknown sessions: " + ", ".join(sorted(chosen - sessions.keys())))
        if related:
            while True:
                extra = set()
                for sid, session in sessions.items():
                    parents = {p for p in (session.get("parent_id"), session.get("fork_session_id")) if p}
                    if sid in chosen:
                        extra.update(parents & sessions.keys())
                    if parents & chosen:
                        extra.add(sid)
                if extra <= chosen:
                    break
                chosen |= extra
        return [sid for sid in sessions if sid in chosen]

    def snapshot(self, sid):
        session = self.db.execute(f'SELECT * FROM "{self.table}" WHERE id=?', (sid,)).fetchone()
        require(session, "Unknown session: " + sid)
        for table in self.unknown_session_tables:
            quoted = table.replace('"', '""')
            require(not self.db.execute(f'SELECT 1 FROM "{quoted}" WHERE session_id=? LIMIT 1', (sid,)).fetchone(),
                    "Unsupported session data table contains records: " + table)
        source = {"session": dict(session)}
        project = self.db.execute("SELECT * FROM project WHERE id=?", (session["project_id"],)).fetchone()
        require(project, "Session project missing: " + sid)
        source["project"] = dict(project)
        for table in SESSION_TABLES:
            source[table] = rows(self.db, f'SELECT * FROM "{table}" WHERE session_id=? ORDER BY rowid', (sid,)) if table in self.tables else []
        for table in ("event_sequence", "event"):
            source[table] = rows(self.db, f'SELECT * FROM "{table}" WHERE aggregate_id=? ORDER BY rowid', (sid,)) if table in self.tables else []
        if self.format == "v2":
            source["session_message"].sort(key=lambda r: r["seq"])
        return source

    def isolate_v1(self, ids, path):
        """Clone schema and selected records only. No account/config/credential rows."""
        require(self.format == "v1", "Only v1 sources need official conversion")
        require(not Path(path).exists(), "Isolation database must be new")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        objects = rows(self.db, "SELECT type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
        with connection(path) as dest:
            for obj in objects:
                if obj["type"] == "table":
                    dest.execute(obj["sql"])
            for table in ("__drizzle_migrations", "migration", "data_migration"):
                if table in self.tables:
                    for row in rows(self.db, f'SELECT * FROM "{table}"'):
                        insert(dest, table, row)
            projects = set()
            workspaces = set()
            for sid in ids:
                snap = self.snapshot(sid)
                if snap["project"]["id"] not in projects:
                    insert(dest, "project", snap["project"])
                    projects.add(snap["project"]["id"])
                wid = snap["session"].get("workspace_id")
                if wid and "workspace" in self.tables and wid not in workspaces:
                    workspace = self.db.execute("SELECT * FROM workspace WHERE id=?", (wid,)).fetchone()
                    require(workspace, "Missing workspace: " + wid)
                    insert(dest, "workspace", dict(workspace))
                    workspaces.add(wid)
                insert(dest, "session", snap["session"])
                for table in (*SESSION_TABLES, "event_sequence", "event"):
                    if table in RUNTIME_INPUTS:
                        continue
                    for row in snap[table]:
                        insert(dest, table, row)
            for obj in objects:
                if obj["type"] in ("index", "trigger", "view"):
                    dest.execute(obj["sql"])
            require(dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Isolated source integrity failure")


def history(source, format):
    """Stable content, excluding shared project activity and session bookkeeping."""
    if format == "v1":
        parts = {}
        for p in source["part"]:
            parts.setdefault(p["message_id"], []).append({"id": p["id"], "data": json.loads(p["data"])})
        return [{"id": m["id"], "data": json.loads(m["data"]), "parts": sorted(parts.get(m["id"], []), key=lambda x: x["id"])}
                for m in sorted(source["message"], key=lambda m: (m["time_created"], m["id"]))]
    return [{"id": m["id"], "type": m["type"], "data": json.loads(m["data"])}
            for m in source["session_message"] if m["type"] in ("user", "assistant")]


def message_time(source, format):
    records = source["message"] if format == "v1" else [m for m in source["session_message"] if m["type"] in ("user", "assistant")]
    return max((m["time_updated"] for m in records), default=0)
