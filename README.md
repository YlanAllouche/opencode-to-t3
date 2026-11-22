# OpenCode to T3 archive importer

Copy OpenCode **1.x or 2.x SQLite conversations** into T3, preserving the IDs used in notes, original source records, and available message attachments. Imports are **archived and settled by default**. They are historical conversations, with no provider sessions, queued jobs, or tool executions attached.

The importer opens OpenCode read-only. For 1.x input, it copies selected conversation records into an isolated database and runs the official **OpenCode 2.0.27** converter there. It never runs v2 against the original database, home, or configuration. For 2.x input, it reads the already-converted records directly.

**Compatibility:** the T3 adapter is tested against **T3 0.1.7**, OpenCode **1.18.29 → 2.0.27**, and the corresponding v2 SQLite storage format. It checks the destination table definitions and projection version before writing. A different schema is rejected until an adapter is validated. This is an independent importer using T3's storage contract; it is not a supported T3 import API.

## Install

Python 3.10 or later is required. The only Python runtime dependency is `t3-archive-core==0.1.0`. The shared core supplies the T3 contract, transaction writer, attachments and reference resolver; this project supplies OpenCode reading/conversion and archive policies.

```sh
python -m pip install .
opencode-t3 --help
```

Install the matching core dependency before running this checkout. For unpublished packages, install a verified `t3-archive-core==0.1.0` wheel first, then use `python -m pip install --no-deps .`. You can also run `python -m opencode_t3` from this checkout with that core installed. No sibling checkout is required.

Only v1 input needs a v2 executable. Install it separately from your normal OpenCode installation, and pass its path explicitly. For example, with npm:

```sh
npm install --prefix "$HOME/.local/share/opencode-t3-tools" --save-exact @opencode/cli@2.0.27
```

Then use `--opencode2 "$HOME/.local/share/opencode-t3-tools/node_modules/.bin/opencode2"` in the import commands. The importer checks the executable version inside its isolated environment. It does not install or upgrade OpenCode automatically.

The real conversion has been exercised on Linux x64. Other platforms need validation; paths are configurable. The default OpenCode input is `$XDG_DATA_HOME/opencode/opencode.db`, falling back to `~/.local/share/opencode/opencode.db`. The default T3 store is `~/.t3/userdata`.

## Select and inspect

```sh
# List conversations from the normal OpenCode location.
opencode-t3 list

# Read another machine's copied database.
opencode-t3 list --input /backups/old-machine/opencode.db

# Inspect one conversation and its related family.
opencode-t3 inspect --input /backups/opencode.db --session ses_example

# Find the existing T3 project that should own the imported archives.
opencode-t3 projects
```

`--input` accepts a SQLite database or a directory containing `opencode.db`. This version does not accept OpenCode JSON exports or the old per-file JSON store.

Without `--session`, import selects all conversations. Repeat `--session` to select several. Selection includes ancestors, children/subagents, and forks transitively by default, so selecting one member may select its entire family. `--no-related` imports only the requested IDs and reports unresolved parent references.

## Stage, validate, or apply

The default operation writes a **new staged copy**, leaving the current T3 store unchanged:

```sh
opencode-t3 import \
  --input /backups/opencode.db \
  --project TARGET_T3_PROJECT_ID \
  --output ./imported-t3 \
  --opencode2 /absolute/path/to/opencode2
```

Omit `--opencode2` for v2 input. Omit `--input` to select the default input. Omit `--output` to create a timestamped directory in the current directory. `--t3-data /path/to/userdata` selects a different existing T3 store. The target project must already exist in that store.

The staged directory contains `statev2.sqlite`, the copied T3 attachment store plus imported attachments, and the environment ID when available. Existing conversations in that database are included. It is a conversation-data staging copy, not a clone of every T3 application setting. Reports contain thread links for the destination environment; staged links become usable there after the staged data is adopted.

To validate the complete database transaction in a disposable copy:

```sh
opencode-t3 import \
  --session ses_example \
  --project TARGET_T3_PROJECT_ID \
  --opencode2 /absolute/path/to/opencode2 \
  --dry-run
```

To update the selected T3 store:

```sh
opencode-t3 import \
  --session ses_example \
  --project TARGET_T3_PROJECT_ID \
  --opencode2 /absolute/path/to/opencode2 \
  --apply
```

Close T3 before an in-place import or adopting a staged copy, so it does not retain an older view of the store. Do not replace an open SQLite database with a staging file. Every in-place import first creates a consistent SQLite backup. A normal T3 backup must include **the database and its attachments directory**; the importer source folder and OpenCode installation are not needed to read imported records afterward.

`--unarchived` imports settled conversations without archiving them. There is no `continue` mode or promise of native OpenCode resumption.

## Duplicate policies

| Policy | Behavior |
| --- | --- |
| `skip` (default) | Leave an existing canonical thread ID alone. No source attachment files or converter are needed for skipped sessions. |
| `prefer-newer` | Update an importer-owned canonical archive only when incoming original history strictly extends the existing history and its latest original message is newer. Save the previous readable version as a separate archived T3 snapshot first. |
| `keep-both` | Keep the canonical copy and import a distinct deterministic snapshot. Repeating the same snapshot adds no events. |

Choose with `--duplicates prefer-newer` or `--duplicates keep-both`.

The canonical archive uses the original OpenCode session ID. Alternate thread IDs have the form `oc-copy-<hash>`, derived from the original ID and a source/attachment fingerprint. Their message, part, and run IDs are remapped too, because those IDs are globally unique in T3. The original IDs remain in the embedded source index, and the resolver returns all matching snapshots. An old bare session ID continues to select the canonical copy; use a snapshot's explicit link to select another version.

Fingerprints exclude import time and source-machine labels, so retrying the same snapshot deduplicates. Provenance records identify the source machine and input path. Use `--source-name old-laptop` to name another machine. Identical imports from another source machine add its provenance to the same archive; repeated imports from that same source add nothing. The `skip` policy leaves existing records entirely alone. Raw source metadata and bytes' hashes are preserved even when native IDs are remapped.

`prefer-newer` never decides a conflict by timestamp alone. It rejects changed earlier messages, changed attachment bytes, edited T3 transcripts, incompatible source formats, and other divergence. Use `keep-both` for those cases. A metadata-only change with identical conversational history also requires `keep-both`. An older prefix is skipped. This policy updates only archives created by this reusable importer; archives from other import tools can be skipped or retained alongside a new snapshot.

## Attachments and moved paths

Attachments are files associated with a message: pasted screenshots, user-supplied files, and explicit assistant/tool file results. The importer does not scan the workspace or interpret paths mentioned in ordinary prose or historical commands.

Inline base64/data URLs are copied into T3-managed attachment files. Explicit local file references are copied from their available paths. For another machine, map those paths:

```sh
opencode-t3 import \
  --input /backups/opencode.db \
  --project TARGET_T3_PROJECT_ID \
  --source-name old-laptop \
  --path-map /home/old-user=/backups/old-user \
  --output ./imported-t3
```

Path maps affect attachment lookup. The original CWD and original paths remain in the embedded source; they do not select a live T3 workspace or cause commands to run.

Missing files, remote-only URLs, invalid encodings, or files outside T3's attachment limits stop the entire import before destination conversation changes commit. No network downloads occur. T3 supports image attachments up to 10 MiB and file attachments from 1 byte to 50 MiB. Larger supported images become file attachments within the file limit; unrecognized image formats use downloadable files.

A file path alone cannot prove what its bytes were when a historical message was written. Such copies are marked `local-file` and reported with a warning. Inline bytes are marked `inline`. Each copied file has a SHA-256 digest. No importer can reconstruct an unavailable original attachment or an already-pruned tool result.

Explicit `outputPath` / `output_path` tool metadata files, known shell capture metadata, and shell capture files referenced by session events are also copied. Arbitrary workspace files, repository contents, git checkpoints, and paths that appear only inside output text are outside this importer’s scope.

## Read archives and resolve old references

An exact original session ID can be passed directly to T3's `t3_thread_read`, including archived discussions. Use `view="activity"` to include tools and reasoning, then follow both position and text pagination. T3's normal search skips archived threads; an empty search does not prove an archive is missing.

For a message ID, part ID, tool-call ID, or saved event ID:

```sh
opencode-t3 resolve msg_example
opencode-t3 resolve call_example --t3-data ./imported-t3
```

Resolution uses only T3's database and includes archived conversations and alternate snapshots. When the environment ID is available, each result includes a complete `t3-thread://v2/...` link. Specify `--environment-id` if a copied store lacks that file. The installed T3 client does not automatically turn bare IDs into links.

Every archive contains an expandable **Complete OpenCode source records** entry. It stores the original v1 records where available, converted/native v2 records, source hashes, original-ID index, provenance, CWD, warnings, and attachment manifest. Helpers appear as expandable work entries rather than ordinary chat replies.

## Fidelity and safety

- Original user/assistant text, reasoning, tools, inputs, outputs, model/provider/variant information, agent, usage, costs, timestamps, and errors are retained in the source records. For v1 imports, visible content is rendered from the originals, so converter rewriting cannot replace the original text.
- Each native T3 run uses its historical model when known. Model changes within a run get an explicit work entry. Full message-level metadata remains in the appendix; native T3 cost accounting is not reconstructed.
- Failed or interrupted tools stay terminal historical entries. Compactions, synthetic messages, step metadata, and unrecognized record types are preserved as expandable entries. A live/running source is captured as a historical snapshot, without starting or continuing its work.
- Parent/subagent and fork relationships are mapped into T3 lineage when their targets are available. Exact fork boundaries stay in the source records; this does not reconstruct executable provider state or materialize an inherited fork prefix as duplicate messages. Read the linked parent and the saved boundary when needed.
- CWD and source project metadata are preserved. Imports belong to the selected existing T3 project; no original worktree or filesystem is recreated.
- Historical queued inputs and saved instruction context are preserved as source records. Queued inputs are not seeded into the converter copy. Unknown session-scoped tables containing data cause an explicit rejection.
- Credential/account/configuration/share-secret table contents are not copied into the isolated converter database. Conversation content can itself contain private information, and reports/backups are private by default.
- All destination conversation writes use one SQLite transaction with event/projection consistency checks. ID collisions, stale projections, attachment failures, and conflicts abort the batch. Newly published files are removed on ordinary failures; a process crash may leave unreferenced attachment files, while SQLite rolls back its uncommitted transaction.
- Existing T3 events are retained when a canonical archive is updated. An additional archived snapshot preserves its earlier source and readable conversation, independent of T3's normal event-log pruning.

## Development

```sh
python -m unittest discover -s tests -v
```

With the pinned core installed, tests use public synthetic conversations and temporary databases. They cover v1 credential-free isolation, v2 reads, multi-part user input, inline and path-mapped attachments, preservation without OpenCode, duplicate policies, lineage, divergence, source immutability, schema drift, stale projections, global ID collisions, and rollback after file publication errors.

`resolve_reference.py` is a source-tree compatibility wrapper; installed users should use `opencode-t3 resolve`.

The pinned core package includes the versioned **schema-only** T3 contract. It has no dependency on extracted application code, a research checkout, private pilot fixtures, or personal paths. The implementation is organized into source reading, isolated conversion, asset copying, rendering, target transactions, and reference resolution modules.
