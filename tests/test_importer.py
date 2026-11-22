from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from opencode_t3.assets import Assets
from opencode_t3.cli import main, parser, run_import
from opencode_t3.common import ImportFailure, connection, encode
from opencode_t3.references import resolve
from opencode_t3.render import alternate_id
from opencode_t3.source import Source
from opencode_t3.target import check_contract, load_bundle
from tests.fixtures import SID, PROJECT, PIXEL, add_message, add_session, extend, t3, v2


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = t3(self.root / "t3")
        self.source = v2(self.root / "opencode.db")
        self.counter = 0

    def args(self, *extra):
        self.counter += 1
        return parser().parse_args(["import", "--input", str(self.source), "--t3-data", str(self.data),
            "--project", PROJECT, "--work-dir", str(self.root / ("work-" + str(self.counter))), "--apply", *extra])

    def run_import(self, *extra):
        return run_import(self.args(*extra))

    def bundle(self, sid=SID):
        with connection(self.data / "statev2.sqlite", readonly=True) as db:
            return load_bundle(db, sid)

    def event_count(self):
        with connection(self.data / "statev2.sqlite", readonly=True) as db:
            return db.execute("SELECT COUNT(*) FROM orchestration_events").fetchone()[0]

    def test_source_unchanged_archived_and_assets_self_contained(self):
        before = self.source.read_bytes()
        report = self.run_import()
        bundle = self.bundle()
        self.assertEqual(before, self.source.read_bytes())
        self.assertTrue(report["validationPassed"])
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertTrue(bundle["thread"]["archivedAt"])
        self.assertEqual(bundle["thread"]["settledOverride"], "settled")
        asset = bundle["archive"]["assets"][0]
        self.assertEqual((self.data / "attachments" / asset["relativePath"]).read_bytes(), PIXEL)
        self.source.unlink()
        self.assertEqual(resolve(self.data / "statev2.sqlite", "call_public")[0]["threadId"], SID)
        self.assertEqual(resolve(self.data / "statev2.sqlite", SID)[0]["threadId"], SID)
        self.assertEqual(bundle["archive"]["cwd"], "/example/original")

    def test_default_skip_does_not_require_attachment_source(self):
        self.run_import()
        count = self.event_count()
        with connection(self.source) as db:
            db.execute("UPDATE session_message SET data=? WHERE id='msg_user'", (encode(dict(text="changed", files=[dict(url="/missing.png")])),))
        self.assertEqual(self.run_import()["results"][0]["result"], "skip")
        self.assertEqual(self.event_count(), count)

    def test_keep_both_is_deterministic_and_remaps_every_global_id(self):
        self.run_import()
        extend(self.source)
        report = self.run_import("--duplicates", "keep-both")
        alternate = report["results"][0]["threadId"]
        self.assertNotEqual(alternate, SID)
        other = self.bundle(alternate)
        first = self.bundle()
        for key in ("runs", "messages", "items"):
            self.assertFalse({x["id"] for x in first[key]} & {x["id"] for x in other[key]})
        self.assertEqual({m["threadId"] for m in resolve(self.data / "statev2.sqlite", SID)}, {SID, alternate})
        matches = resolve(self.data / "statev2.sqlite", "msg_user")
        self.assertEqual({m["threadId"] for m in matches}, {SID, alternate})
        count = self.event_count()
        report = self.run_import("--duplicates", "keep-both")
        self.assertEqual(report["results"][0]["threadId"], alternate)
        self.assertEqual(report["results"][0]["result"], "identical-snapshot")
        self.assertEqual(self.event_count(), count)
        for asset in other["archive"]["assets"]:
            self.assertTrue(asset["attachment"]["id"].startswith(alternate))

    def test_prefer_newer_keeps_old_readable_snapshot_in_t3(self):
        self.run_import()
        old = deepcopy(self.bundle())
        extend(self.source)
        report = self.run_import("--duplicates", "prefer-newer")
        snapshot_id = report["results"][0]["previousSnapshotId"]
        self.assertEqual(snapshot_id, alternate_id(SID, old["archive"]["sourceFingerprint"]))
        self.assertEqual(len(self.bundle()["runs"]), 2)
        self.assertEqual(len(self.bundle(snapshot_id)["runs"]), 1)
        self.assertEqual(len(resolve(self.data / "statev2.sqlite", "msg_user")), 2)
        self.assertEqual(self.bundle()["runs"][1]["modelSelection"]["model"], "anthropic/new-model")
        self.assertEqual(self.bundle()["runs"][1]["status"], "interrupted")
        count = self.event_count()
        self.assertEqual(self.run_import("--duplicates", "prefer-newer")["results"][0]["result"], "identical")
        self.assertEqual(self.event_count(), count)

    def test_divergence_not_decided_by_latest_timestamp(self):
        self.run_import()
        extend(self.source)
        with connection(self.source) as db:
            db.execute("UPDATE session_message SET data=? WHERE id='msg_user'", (encode(dict(text="Different branch")),))
        count = self.event_count()
        with self.assertRaisesRegex(ImportFailure, "Divergent history"):
            self.run_import("--duplicates", "prefer-newer")
        self.assertEqual(self.event_count(), count)

    def test_changed_asset_is_conflict_even_when_history_extends(self):
        image = self.root / "source.png"
        image.write_bytes(PIXEL)
        with connection(self.source) as db:
            db.execute("UPDATE session_message SET data=? WHERE id='msg_user'", (encode(dict(text="Inspect this screenshot", files=[dict(url=str(image), mime="image/png", name="source.png")])),))
        self.run_import()
        extend(self.source)
        image.write_bytes(PIXEL + b"changed")
        with self.assertRaisesRegex(ImportFailure, "Attachment bytes changed"):
            self.run_import("--duplicates", "prefer-newer")

    def test_missing_attachment_rolls_back_whole_batch(self):
        with connection(self.source) as db:
            add_session(db, "ses_missing")
            add_message(db, "ses_missing", "msg_missing", "user", 1, dict(text="Look", files=[dict(url="/nonexistent/import-image.png")]))
        with self.assertRaisesRegex(ImportFailure, "Attachment file is missing"):
            self.run_import()
        self.assertEqual(self.event_count(), 0)
        self.assertIsNone(self.bundle())
        self.assertEqual(list((self.data / "attachments").iterdir()), [])

    def test_dry_run_does_not_touch_destination(self):
        args = self.args()
        args.apply, args.dry_run = False, True
        before = (self.data / "statev2.sqlite").read_bytes()
        report = run_import(args)
        self.assertFalse(report["targetCommitted"])
        self.assertTrue(report["validationPassed"])
        self.assertEqual((self.data / "statev2.sqlite").read_bytes(), before)
        self.assertEqual(list((self.data / "attachments").iterdir()), [])

    def test_default_stage_is_complete_separate_store(self):
        self.run_import()
        args = self.args()
        args.apply = False
        args.output = self.root / "staged"
        count = self.event_count()
        report = run_import(args)
        self.assertEqual(Path(report["target"]), args.output / "statev2.sqlite")
        self.assertEqual(self.event_count(), count)
        self.assertEqual(sorted(p.name for p in (self.data / "attachments").iterdir()), sorted(p.name for p in (args.output / "attachments").iterdir()))

    def test_projection_lag_and_schema_drift_rejected(self):
        with connection(self.data / "statev2.sqlite") as db:
            db.execute("UPDATE orchestration_v2_projection_metadata SET last_sequence=7")
        with self.assertRaisesRegex(ImportFailure, "behind events"):
            self.run_import()
        with connection(self.data / "statev2.sqlite") as db:
            db.execute("UPDATE orchestration_v2_projection_metadata SET last_sequence=0")
            db.execute("ALTER TABLE orchestration_v2_projection_messages ADD COLUMN future_field TEXT")
        with self.assertRaisesRegex(ImportFailure, "Unsupported T3 schema"):
            self.run_import()

    def test_parent_fork_closure_and_lineage(self):
        with connection(self.source) as db:
            add_session(db, "ses_child", parent=SID)
            add_session(db, "ses_fork", fork=SID)
            add_message(db, "ses_child", "msg_child", "user", 1, dict(text="Subagent task"))
            add_message(db, "ses_fork", "msg_fork", "user", 1, dict(text="Fork task"))
        self.run_import("--session", "ses_child")
        self.assertEqual(self.bundle("ses_child")["thread"]["lineage"], dict(parentThreadId=SID, relationshipToParent="subagent", rootThreadId=SID))
        fork = self.bundle("ses_fork")
        self.assertEqual(fork["thread"]["lineage"]["relationshipToParent"], "fork")
        self.assertEqual(fork["archive"]["sourceV2"]["session"]["fork_boundary"], "msg_user")

    def test_failed_tool_compaction_and_unknown_message_are_retained(self):
        extend(self.source)
        with connection(self.source) as db:
            add_message(db, SID, "msg_compact", "compaction", 5, dict(status="completed", summary="Older summary", recent="Recent context"))
            add_message(db, SID, "msg_future", "future-type", 6, dict(payload=[1, 2, 3]))
        self.run_import()
        items = self.bundle()["items"]
        self.assertTrue(any(i.get("title") == "OpenCode compaction" and i["output"]["summary"] == "Older summary" for i in items))
        self.assertTrue(any(i.get("title") == "OpenCode future-type" and i["output"]["payload"] == [1, 2, 3] for i in items))
        self.assertTrue(any(i.get("toolName") == "bash" and i["status"] == "failed" for i in items))

    def test_path_map_and_remote_attachment_fail_closed(self):
        file = self.root / "moved.png"
        file.write_bytes(PIXEL)
        assets = Assets(self.root / "assets", [("/old-machine", str(self.root))])
        self.assertEqual(assets.read(dict(url="file:///old-machine/moved.png"))[0], PIXEL)
        with self.assertRaisesRegex(ImportFailure, "Remote attachment"):
            assets.read(dict(url="https://example.org/image.png"))

    def test_edited_t3_transcript_is_not_overwritten(self):
        self.run_import()
        with connection(self.data / "statev2.sqlite") as db:
            row = db.execute("SELECT payload_json FROM orchestration_v2_projection_messages WHERE message_id='msg_answer'").fetchone()
            msg = json.loads(row[0]); msg["text"] = "Edited in T3"
            db.execute("UPDATE orchestration_v2_projection_messages SET payload_json=? WHERE message_id='msg_answer'", (encode(msg),))
        extend(self.source)
        with self.assertRaisesRegex(ImportFailure, "edited in T3"):
            self.run_import("--duplicates", "prefer-newer")

    def test_tool_files_are_copied_and_historical_commands_are_never_run(self):
        import base64
        from tests.fixtures import add_message
        should_not_exist = self.root / "must-not-be-created"
        with connection(self.source) as db:
            add_message(db, SID, "msg_tools", "assistant", 3, dict(model=dict(providerID="openai", id="old-model"), content=[
                dict(type="tool", id="call_command", name="bash", state=dict(status="completed", input=dict(command="touch " + str(should_not_exist)),
                    metadata=dict(exit=0), content=[dict(type="text", text="Historical command output")])),
                dict(type="tool", id="call_image", name="image-result", state=dict(status="completed", input={}, content=[
                    dict(type="file", uri="data:image/png;base64," + base64.b64encode(PIXEL).decode(), mime="image/png", name="result.png")]))]))
        self.run_import()
        bundle = self.bundle()
        self.assertFalse(should_not_exist.exists())
        self.assertTrue(any(i["type"] == "command_execution" and i["input"] == "touch " + str(should_not_exist) for i in bundle["items"]))
        self.assertEqual(len(bundle["archive"]["assets"]), 2)
        self.assertEqual(bundle["archive"]["warnings"], [])
        self.assertTrue(all((self.data / "attachments" / a["relativePath"]).read_bytes() == PIXEL for a in bundle["archive"]["assets"]))

    def test_identical_snapshot_from_another_machine_keeps_both_origins(self):
        self.run_import("--source-name", "machine-one")
        report = self.run_import("--duplicates", "keep-both", "--source-name", "machine-two")
        self.assertEqual(report["results"][0]["result"], "identical")
        self.assertEqual(report["addedEvents"], 1)
        self.assertEqual({p["sourceName"] for p in self.bundle()["archive"]["provenance"]}, {"machine-one", "machine-two"})
        count = self.event_count()
        self.run_import("--duplicates", "keep-both", "--source-name", "machine-two")
        self.assertEqual(self.event_count(), count)

    def test_cli_errors_do_not_emit_traceback(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["list", "--input", str(self.root / "missing")])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
