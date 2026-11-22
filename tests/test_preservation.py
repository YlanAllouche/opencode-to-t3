from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencode_t3.assets import Assets
from opencode_t3.cli import run_import
from opencode_t3.common import ImportFailure, connection, encode, insert
from opencode_t3.converter import environment
from opencode_t3.render import build
from opencode_t3.source import Source
from tests.fixtures import SID, PROJECT, PIXEL, add_message, add_session, v1
from tests import test_importer as integration


class AdditionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_v1_snapshot_copies_no_credentials(self):
        source_path = v1(self.root / "v1.db")
        before = source_path.read_bytes()
        with Source(source_path) as source:
            source.isolate_v1([SID], self.root / "isolated.db")
            snapshot = source.snapshot(SID)
        with connection(self.root / "isolated.db", readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credential").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM part").fetchone()[0], 7)
        self.assertEqual(source_path.read_bytes(), before)
        # Render originals even when a converter produces different text/tool output.
        converted = deepcopy(snapshot)
        converted["session_message"] = [dict(id=m["id"], type=json.loads(m["data"])["role"], time_created=m["time_created"],
            time_updated=m["time_updated"], data=encode(dict(text="Changed by converter"))) for m in snapshot["message"]]
        assets = Assets(self.root / "assets")
        bundle = build(snapshot, converted, "v1", PROJECT, SID, assets, dict(sourceName="test"))
        self.assertEqual(bundle["messages"][0]["text"], "First user part")
        self.assertTrue(any(i.get("title") == "OpenCode synthetic text" and i["output"]["text"] == "Second user part" for i in bundle["items"]))
        self.assertTrue(any(i.get("text") == "Original reasoning" for i in bundle["items"]))
        self.assertTrue(any(i.get("output") == "Exact v1 output" for i in bundle["items"]))
        self.assertIn("call_v1", bundle["archive"]["references"])
        self.assertEqual(bundle["archive"]["sourceV1"], snapshot)
        self.assertEqual((assets.root / bundle["archive"]["assets"][0]["relativePath"]).read_bytes(), PIXEL)

    def test_isolated_environment_strips_keys_and_config(self):
        with patch.dict("os.environ", dict(OPENAI_API_KEY="synthetic", OPENCODE_CONFIG="secret", XDG_CONFIG_HOME="/original/config", NODE_OPTIONS="--inspect")):
            env = environment(self.root / "isolated")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENCODE_CONFIG", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotEqual(env["XDG_CONFIG_HOME"], "/original/config")
        self.assertTrue(Path(env["OPENCODE_DB"]).is_relative_to(self.root))

    def test_incomplete_v2_migration_is_rejected(self):
        from tests.fixtures import v2
        path = v2(self.root / "v2.db")
        with connection(path) as db:
            db.execute("UPDATE kv SET value=?", (encode(dict(phase="running")),))
        with self.assertRaisesRegex(ImportFailure, "incomplete"):
            Source(path)

    def test_sql_id_collision_rolls_back_preceding_session_and_events(self):
        case = integration.ImporterTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        with connection(case.source) as db:
            add_session(db, "ses_second")
            add_message(db, "ses_second", "msg_collision", "user", 1, dict(text="Second discussion"))
        with connection(case.data / "statev2.sqlite") as db:
            insert(db, "orchestration_v2_projection_messages", dict(message_id="msg_collision", thread_id="other-thread", run_id=None,
                node_id=None, role="user", streaming=0, created_at="2023-01-01T00:00:00.000Z", updated_at="2023-01-01T00:00:00.000Z", payload_json="{}"))
        with self.assertRaisesRegex(ImportFailure, "Global ID collision"):
            case.run_import()
        self.assertEqual(case.event_count(), 0)
        self.assertIsNone(case.bundle())
        self.assertEqual(list((case.data / "attachments").iterdir()), [])

    def test_attachment_publish_failure_rolls_back_database(self):
        case = integration.ImporterTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        with patch("opencode_t3.cli.publish_assets", side_effect=OSError("Synthetic disk full")):
            with self.assertRaisesRegex(ImportFailure, "Synthetic disk full"):
                case.run_import()
        self.assertEqual(case.event_count(), 0)
        self.assertIsNone(case.bundle())

    def test_non_related_selection_records_missing_parent(self):
        case = integration.ImporterTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        with connection(case.source) as db:
            add_session(db, "ses_child", parent=SID)
            add_message(db, "ses_child", "msg_child", "user", 1, dict(text="Task"))
        report = case.run_import("--session", "ses_child", "--no-related")
        self.assertEqual(report["results"][0]["unresolvedRelations"], [SID])
        self.assertIsNone(case.bundle())
        self.assertEqual(case.bundle("ses_child")["thread"]["lineage"]["parentThreadId"], None)

    def test_unknown_session_table_with_records_is_rejected(self):
        from tests.fixtures import v2
        path = v2(self.root / "v2.db")
        with connection(path) as db:
            db.execute("CREATE TABLE future_session_data(session_id TEXT, data TEXT)")
            db.execute("INSERT INTO future_session_data VALUES (?,?)", (SID, "must not be lost"))
        with Source(path) as source:
            with self.assertRaisesRegex(ImportFailure, "future_session_data"):
                source.snapshot(SID)

    def test_pending_input_is_archived_but_not_seeded_into_converter(self):
        path = v1(self.root / "v1.db")
        with connection(path) as db:
            db.execute("CREATE TABLE session_input(id TEXT PRIMARY KEY, session_id TEXT, prompt TEXT)")
            db.execute("INSERT INTO session_input VALUES (?,?,?)", ("input_pending", SID, "Historical queued instruction"))
        with Source(path) as source:
            self.assertEqual(source.snapshot(SID)["session_input"][0]["prompt"], "Historical queued instruction")
            source.isolate_v1([SID], self.root / "isolated.db")
        with connection(self.root / "isolated.db", readonly=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_input").fetchone()[0], 0)

    def test_explicit_shell_capture_file_and_event_ids_survive(self):
        case = integration.ImporterTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        capture = self.root / "shell.out"
        capture.write_bytes(b"Historical shell output")
        with connection(case.source) as db:
            db.execute("CREATE TABLE event(id TEXT PRIMARY KEY, aggregate_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            db.execute("INSERT INTO event VALUES (?,?,?,?,?)", ("evt_shell", SID, "session.shell.started", 1,
                encode(dict(shell=dict(id="sh_public", command="historical-only", file=str(capture))))))
            add_message(db, SID, "msg_shell", "shell", 3, dict(shellID="sh_public", command="historical-only"))
        report = case.run_import()
        bundle = case.bundle()
        shell_asset = next(a for a in bundle["archive"]["assets"] if a["owner"] == "evt_shell")
        self.assertEqual((case.data / "attachments" / shell_asset["relativePath"]).read_bytes(), capture.read_bytes())
        from opencode_t3.references import resolve
        for ref in ("evt_shell", "sh_public"):
            self.assertEqual(resolve(case.data / "statev2.sqlite", ref)[0]["threadId"], SID)
        index = next(m for m in bundle["messages"] if m["id"].endswith(":migration-index"))
        self.assertIn(shell_asset["attachment"], index["attachments"])
