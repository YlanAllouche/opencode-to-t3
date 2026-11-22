"""Synthetic fixtures only; no personal conversation or installed app required."""
import base64
import json
from pathlib import Path

from opencode_t3.common import connection, encode, insert
from opencode_t3.target import CONTRACT

T = 1700000000000
SID = "ses_publicFixture"
PROJECT = "public-project"
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")


def t3(root):
    root = Path(root)
    root.mkdir(parents=True)
    with connection(root / "statev2.sqlite") as db:
        for sql in CONTRACT["tables"].values():
            db.execute(sql)
        insert(db, "projection_projects", dict(project_id=PROJECT, title="Public fixture", workspace_root="/example/workspace",
            scripts_json="[]", created_at="2023-11-14T22:13:20.000Z", updated_at="2023-11-14T22:13:20.000Z", deleted_at=None))
        insert(db, "orchestration_v2_projection_metadata", dict(projection_name="thread-projections", schema_version=2, last_sequence=0, updated_at="2023-11-14T22:13:20.000Z"))
    (root / "environment-id").write_text("public-environment")
    (root / "attachments").mkdir()
    return root


def v2(path, *, sid=SID, attachment=True):
    path = Path(path)
    with connection(path) as db:
        db.executescript('''
        CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, time_updated INTEGER);
        CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE session_v2(id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, fork_session_id TEXT,
            fork_boundary TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
        ''')
        insert(db, "project", dict(id="source-project", worktree="/example/original", time_updated=T))
        insert(db, "kv", dict(key="migration.v1-v2", value=encode(dict(phase="completed"))))
        add_session(db, sid)
        files = [dict(data=base64.b64encode(PIXEL).decode(), mime="image/png", name="screenshot.png", source=dict(type="inline"))] if attachment else []
        add_message(db, sid, "msg_user", "user", 1, dict(text="Inspect this screenshot", files=files))
        add_message(db, sid, "msg_answer", "assistant", 2, dict(agent="plan", model=dict(providerID="openai", id="old-model", variant="high"),
            content=[dict(type="reasoning", text="Historical reasoning"), dict(type="text", text="A historical answer"),
                dict(type="tool", id="call_public", name="read", state=dict(status="completed", input=dict(path="/example/file"), content=[dict(type="text", text="Full tool output")]))]))
    return path


def add_session(db, sid, parent=None, fork=None):
    insert(db, "session_v2", dict(id=sid, project_id="source-project", parent_id=parent, fork_session_id=fork,
        fork_boundary="msg_user" if fork else None, title="Synthetic discussion", directory="/example/original", time_created=T, time_updated=T+100))


def add_message(db, sid, mid, kind, seq, raw):
    insert(db, "session_message", dict(id=mid, session_id=sid, type=kind, seq=seq, time_created=T+seq*10, time_updated=T+seq*10+1, data=encode(raw)))


def extend(path):
    with connection(path) as db:
        add_message(db, SID, "msg_user_2", "user", 3, dict(text="Next question"))
        add_message(db, SID, "msg_answer_2", "assistant", 4, dict(agent="build", model=dict(providerID="anthropic", id="new-model"),
            content=[dict(type="tool", id="call_failed", name="bash", state=dict(status="error", input=dict(command="historical-only"), error=dict(message="Historical failure"))),
                     dict(type="text", text="Another answer")]))
        db.execute("UPDATE session_v2 SET time_updated=? WHERE id=?", (T+200, SID))


def v1(path):
    with connection(path) as db:
        db.executescript('''
        CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, time_updated INTEGER);
        CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part(id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE account(id TEXT PRIMARY KEY, secret TEXT);
        CREATE TABLE credential(id TEXT PRIMARY KEY, secret TEXT);
        CREATE TABLE migration(id TEXT PRIMARY KEY,time_completed INTEGER);
        ''')
        insert(db, "project", dict(id="source-project", worktree="/example/original", time_updated=T))
        insert(db, "session", dict(id=SID, project_id="source-project", parent_id=None, title="Public v1 fixture", directory="/example/original", time_created=T, time_updated=T+100))
        insert(db, "account", dict(id="do-not-copy", secret="synthetic-secret"))
        insert(db, "credential", dict(id="do-not-copy", secret="synthetic-key"))
        for mid, role, seq in (("msg_user", "user", 1), ("msg_answer", "assistant", 2)):
            raw = dict(role=role)
            if role == "assistant":
                raw.update(providerID="openai", modelID="historical-model", variant="high")
            insert(db, "message", dict(id=mid, session_id=SID, time_created=T+seq, time_updated=T+seq, data=encode(raw)))
        for n, (mid, data) in enumerate([
            ("msg_user", dict(type="text", text="First user part")),
            ("msg_user", dict(type="text", text="Second user part", synthetic=True)),
            ("msg_user", dict(type="file", url="data:image/png;base64,"+base64.b64encode(PIXEL).decode(), mime="image/png", filename="paste.png")),
            ("msg_answer", dict(type="reasoning", text="Original reasoning")),
            ("msg_answer", dict(type="text", text="Original answer")),
            ("msg_answer", dict(type="tool", callID="call_v1", tool="read", state=dict(status="completed", input=dict(filePath="/example/file"), output="Exact v1 output"))),
            ("msg_answer", dict(type="step-finish", cost=0.5, tokens=dict(input=10, output=3))),
        ]):
            insert(db, "part", dict(id="prt_public_"+str(n), session_id=SID, message_id=mid, time_created=T+n, time_updated=T+n, data=encode(data)))
    return Path(path)
