"""Render historical records into T3's inert conversation/event contract."""
from collections import defaultdict
from copy import deepcopy
import json

from .common import digest, iso, require
from .source import history, message_time
from t3_archive_core.events import native_digest, refresh

FORMAT = "opencode-t3-archive-v2"


def model_of(raw):
    model = raw.get("model")
    if not isinstance(model, dict):
        model = {"providerID": raw.get("providerID"), "id": raw.get("modelID")}
    name = model.get("id") or model.get("modelID")
    if not name:
        return None
    selection = dict(instanceId="opencode", model=(model.get("providerID") or "unknown") + "/" + name)
    if model.get("variant") or raw.get("variant"):
        selection["options"] = [dict(id="variant", value=model.get("variant") or raw["variant"])]
    return selection


def alternate_id(sid, fingerprint):
    # App IDs are global, including message/part IDs. Remap those as well.
    return "oc-copy-" + digest([sid, fingerprint])[:40]






def build(source, converted, source_format, project_id, target_id, assets, provenance, *, archived=True, lineage=None):
    original_sid = source["session"]["id"]
    sid = target_id
    session = source["session"]
    start = iso(session["time_created"])
    end = iso(max([session["time_updated"], *[r["time_updated"] for r in converted["session_message"]]]))
    remap = (lambda key: key if sid == original_sid else sid + "-" + digest(key)[:32])
    models = [model_of(json.loads(m["data"])) for m in converted["session_message"] if m["type"] == "assistant"]
    models = [m for m in models if m]
    default_model = models[0] if models else dict(instanceId="opencode", model="unknown")
    active_model = deepcopy(default_model)
    thread = dict(createdBy="system", creationSource="server", id=sid, projectId=project_id,
                  title="[OpenCode archive] " + (session.get("title") or original_sid), providerInstanceId="opencode",
                  modelSelection=models[-1] if models else default_model, runtimeMode="approval-required", interactionMode="default",
                  branch=None, worktreePath=None, activeProviderThreadId=None, historyOrigin="native",
                  lineage=lineage or dict(parentThreadId=None, relationshipToParent=None, rootThreadId=sid), forkedFrom=None,
                  createdAt=start, updatedAt=end, archivedAt=end if archived else None, settledOverride="settled", settledAt=end,
                  unsettledAt=None, snoozedUntil=None, snoozedAt=None, pinnedAt=None, pinOrderKey=None,
                  activeOrderKey=None, lastVisitedAt=None, titleRegeneration=None, deletedAt=None)
    runs, messages, items = [], [], []
    references = {original_sid: {"threadId": sid}}
    current = None
    record_start = len(assets.records)
    warnings = []
    appendix_id = sid + ":source-appendix"

    def base(key, created, updated):
        return dict(id=key, threadId=sid, runId=current["id"] if current else None, nodeId=None,
                    providerThreadId=None, providerTurnId=None, nativeItemRef=None, parentItemId=None,
                    ordinal=len(items)+1, status="completed", title=None, startedAt=created, completedAt=updated, updatedAt=updated)

    def work(key, title, output, created, updated, tool="opencode_archive_record"):
        item = dict(base(key, created, updated), type="dynamic_tool", toolName=tool, input={}, output=output, title=title)
        items.append(item)
        return item

    def new_message(mid, role, text, created, updated, attachments=(), created_by=None):
        message = dict(createdBy=created_by or ("user" if role == "user" else "agent"), creationSource="server", id=remap(mid),
                       threadId=sid, runId=current["id"] if current else None, nodeId=None, role=role, text=text,
                       attachments=list(attachments), streaming=False, createdAt=created, updatedAt=updated)
        messages.append(message)
        references[mid] = {"messageId": message["id"]}
        return message

    if source_format == "v1":
        # Render originals directly: conversion never substitutes pruned/reworded text.
        owned = defaultdict(list)
        for p in sorted(source["part"], key=lambda p: (p["time_created"], p["id"])):
            owned[p["message_id"]].append(p)
        entries = []
        converted_ids = {m["id"] for m in converted["session_message"]}
        require({m["id"] for m in source["message"]} <= converted_ids, "Official converter omitted source messages")
        for row in sorted(source["message"], key=lambda m: (m["time_created"], m["id"])):
            raw = json.loads(row["data"])
            parts = owned[row["id"]]
            entries.append((row, raw.get("role", "unknown"), raw, [(p["id"], json.loads(p["data"])) for p in parts]))
        original_ids = {m["id"] for m in source["message"]}
        for n, m in enumerate(m for m in converted["session_message"] if m["id"] not in original_ids):
            entries.append(({**m, "id": original_sid + ":conversion:" + str(n)}, m["type"], json.loads(m["data"]), []))
    else:
        entries = []
        for row in converted["session_message"]:
            raw = json.loads(row["data"])
            parts = [(row["id"] + ":content:" + str(n), part) for n, part in enumerate(raw.get("content", []))]
            entries.append((row, row["type"], raw, parts))

    for row, typ, raw, parts in entries:
        mid = row["id"]
        created, updated = iso(row["time_created"]), iso(row["time_updated"])
        files = list(raw.get("files") or []) if typ == "user" else []
        if typ == "user":
            files += [part for _, part in parts if part["type"] == "file"]
            attachments = [assets.copy(sid, mid, n, f) for n, f in enumerate(files)]
            text_parts = [(pid, p) for pid, p in parts if p["type"] == "text" and not p.get("synthetic") and not p.get("ignored")]
            text = raw.get("text", "\n".join(p["text"] for _, p in text_parts))
            current = dict(id=sid + ":run:" + str(len(runs)+1), threadId=sid, ordinal=len(runs)+1,
                           providerInstanceId="opencode", modelSelection=deepcopy(active_model), providerThreadId=None,
                           userMessageId=remap(mid), rootNodeId=None, activeAttemptId=None, status="completed", queuePosition=None,
                           requestedAt=created, startedAt=created, completedAt=updated, checkpointId=None, contextHandoffId=None)
            runs.append(current)
            msg = new_message(mid, "user", text, created, updated, attachments)
            key = remap(text_parts[0][0]) if text_parts else remap(mid) + ":item"
            items.append(dict(base(key, created, updated), type="user_message", messageId=msg["id"], text=text,
                              createdBy="user", creationSource="server", inputIntent="turn_start", attachments=attachments))
            for pid, p in parts:
                if p["type"] in ("text", "file") and not p.get("synthetic") and not p.get("ignored"):
                    references[pid] = {"itemId": key, "sourcePartId": pid}
                else:
                    label = "OpenCode " + ("synthetic " if p.get("synthetic") else "ignored " if p.get("ignored") else "") + p["type"]
                    item = work(remap(pid), label, p, created, updated)
                    references[pid] = {"itemId": item["id"]}
        elif typ == "assistant":
            selected_model = model_of(raw)
            if selected_model:
                active_model = selected_model
            if current and selected_model:
                if not any(m["runId"] == current["id"] and m["role"] == "assistant" for m in messages):
                    current["modelSelection"] = selected_model
                elif selected_model != current["modelSelection"]:
                    work(remap(mid) + ":model", "Historical model change", {"model": selected_model, "agent": raw.get("agent")}, created, updated)
            text = "\n".join(p["text"] for _, p in parts if p["type"] == "text")
            # Tools may return attachments too. Keep them on the backing message and an explicit work entry.
            assistant_attachments = []
            msg = new_message(mid, "assistant", text, created, updated)
            for pid, part in parts:
                kind = part["type"]
                key = remap(pid)
                item = base(key, created, updated)
                if kind in ("text", "reasoning"):
                    item.update(type="assistant_message" if kind == "text" else "reasoning", text=part["text"], streaming=False)
                    if kind == "text":
                        item["messageId"] = msg["id"]
                elif kind == "tool":
                    state = part["state"]
                    tool = part.get("tool") or part.get("name") or "unknown"
                    call_id = part.get("callID") or part.get("id")
                    output = state.get("output")
                    if output is None:
                        output = "\n".join(c["text"] for c in state.get("content", []) if c["type"] == "text")
                    if state.get("error"):
                        output = dict(output=output, error=state["error"])
                    tool_files = list(state.get("attachments") or []) + [c for c in state.get("content", []) if c["type"] == "file"]
                    copied = [assets.copy(sid, pid, n, f) for n, f in enumerate(tool_files)]
                    assistant_attachments.extend(copied)
                    if copied:
                        output = dict(output=output, attachments=copied)
                    status = state["status"]
                    terminal = "completed" if status == "completed" else "failed" if status in ("error", "failed") else "interrupted"
                    item.update(type="dynamic_tool", toolName=tool, input=state.get("input", {}), output=output,
                                title=state.get("title") or tool, status=terminal)
                    if current and terminal != "completed":
                        current["status"] = "interrupted"
                    if call_id:
                        references[call_id] = {"itemId": key}
                    metadata = state.get("metadata") or {}
                    if metadata.get("truncated"):
                        warnings.append("Source tool output was already truncated: " + pid)
                    for field in ("outputPath", "output_path", *(["file"] if metadata.get("shellID") else [])):
                        if metadata.get(field):
                            copied_file = assets.copy(sid, pid + ":full-output", 0, {"url": metadata[field], "name": "full-output.txt", "mime": "text/plain"})
                            assistant_attachments.append(copied_file)
                            item["output"] = {"output": item["output"], "fullOutput": copied_file}
                    if tool == "bash" and isinstance(state.get("input"), dict) and isinstance(state["input"].get("command"), str) and isinstance(item["output"], str):
                        item.update(type="command_execution", input=state["input"]["command"])
                        item.pop("toolName")
                        code = metadata.get("exit", metadata.get("exitCode"))
                        if type(code) is int:
                            item["exitCode"] = code
                elif kind == "file":
                    copied = assets.copy(sid, pid, 0, part)
                    assistant_attachments.append(copied)
                    item.update(type="dynamic_tool", toolName="opencode_archive_attachment", input={}, output=copied, title=copied["name"])
                else:
                    item.update(type="dynamic_tool", toolName="opencode_archive_record", input={}, output=part, title="OpenCode " + kind)
                items.append(item)
                references[pid] = {"itemId": key}
            msg["attachments"] = assistant_attachments
            if raw.get("error"):
                work(remap(mid) + ":error", "Historical response error", raw["error"], created, updated)
                if current:
                    current["status"] = "interrupted"
        else:
            # Compactions, model/agent changes, synthetic messages, idle markers, future types.
            new_message(mid, "system", raw.get("text", ""), created, updated, created_by="system")
            work(remap(mid) + ":item", "OpenCode " + typ, raw, created, updated)
            if typ == "idle" and current and raw.get("outcome") in ("failed", "interrupted"):
                current["status"] = raw["outcome"]
        if current:
            current["completedAt"] = max(current["completedAt"], updated)

    shell_outputs = set()
    captured_shells = set()
    for event in [*source.get("event", []), *converted.get("event", [])]:
        if event["id"] in shell_outputs or not event.get("type", "").startswith("session.shell."):
            continue
        payload = json.loads(event["data"])
        shell = payload.get("shell") or (payload.get("data") or {}).get("shell")
        if not isinstance(shell, dict) or not shell.get("file"):
            continue
        shell_outputs.add(event["id"])
        attachment = assets.copy(sid, event["id"], 0, {"url": shell["file"], "name": "shell-output.txt", "mime": "text/plain"})
        item = work(remap(event["id"]) + ":output", "Historical shell output", {"attachment": attachment, "shell": shell}, start, end)
        references[event["id"]] = {"itemId": item["id"]}
        if shell.get("id"):
            captured_shells.add(shell["id"])
            references[shell["id"]] = {"itemId": item["id"]}
    for snapshot in (source, converted):
        for table, records in snapshot.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and record.get("id"):
                    references.setdefault(record["id"], {"itemId": appendix_id, "sourceRecordId": record["id"], "sourceTable": table})
    for row, typ, raw, _ in entries:
        if typ == "shell" and raw.get("shellID"):
            references.setdefault(raw["shellID"], {"itemId": remap(row["id"]) + ":item"})
            if raw["shellID"] not in captured_shells:
                warnings.append("Historical shell may have external output unavailable in the database: " + raw["shellID"])
    asset_records = assets.records[record_start:]
    for a in asset_records:
        if a["byteSource"] == "local-file":
            warnings.append("Attachment copied from a local file; original historical bytes cannot be verified: " + a["owner"])
    hist = history(source, source_format)
    # Include all raw session records and asset bytes' hashes in snapshot identity.
    # Exclude shared project activity only; model/title/lineage changes still produce a new snapshot.
    stable = deepcopy(source)
    stable["project"].pop("time_updated", None)
    fingerprint = digest(dict(format=source_format, source=stable, assets=[a["sha256"] for a in asset_records]))
    archive = dict(format=FORMAT, sourceFormat=source_format, sourceV1=source if source_format == "v1" else None,
                   sourceV2=converted, sourceV1Sha256=digest(source) if source_format == "v1" else None,
                   sourceV2Sha256=digest(converted), originalSessionId=original_sid, references=references,
                   sourceFingerprint=fingerprint, history=hist, latestMessageAt=message_time(source, source_format),
                   provenance=[provenance], cwd=session.get("directory"), assets=asset_records, warnings=warnings)
    notice = "Imported OpenCode archive. Original session ID: " + original_sid + ".\nSource IDs:\n" + "\n".join(references)
    mid = sid + ":migration-index"
    # No remapping: this is an importer-owned ID, already scoped to target thread.
    messages.append(dict(createdBy="system", creationSource="server", id=mid, threadId=sid,
                         runId=current["id"] if current else None, nodeId=None, role="assistant", text=notice,
                         attachments=[a["attachment"] for a in asset_records], streaming=False, createdAt=end, updatedAt=end))
    work(mid + ":item", "Import details and source IDs", notice, end, end, "opencode_archive_index")
    work(appendix_id, "Complete OpenCode source records", archive, end, end, "opencode_archive_source")
    return refresh(dict(thread=thread, runs=runs, messages=messages, items=items, archive=archive))
