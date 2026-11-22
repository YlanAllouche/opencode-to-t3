"""OpenCode archive policies and compatibility exports for the shared writer."""
from copy import deepcopy
from pathlib import Path
from .common import digest, require
from .render import FORMAT, alternate_id, refresh
from t3_archive_core.target import (CONTRACT, EXECUTABLE, PROJECTIONS, check_contract, load_bundle,
    assert_inert, keyed_native_digest, stamp_digest, merge_provenance, finish)
from t3_archive_core.target import store as _store


def store(db, bundle, replace=False):
    return _store(db, bundle, replace=replace, driver="opencode")


def choose(existing, incoming, policy):
    if existing is None:
        return "import"
    if policy == "skip":
        return "skip"
    old, new = existing["archive"], incoming["archive"]
    if old and old.get("format") == FORMAT and old.get("sourceFingerprint") == new["sourceFingerprint"]:
        return "identical"
    if policy == "keep-both":
        return "alternate"
    require(old and old.get("format") == FORMAT, "prefer-newer can only update archives created by this importer; use keep-both")
    a, b = old["history"], new["history"]
    if b == a:
        require(old["sourceFingerprint"] == new["sourceFingerprint"],
                "Same conversation but changed metadata or attachment bytes; use keep-both")
        return "identical"
    if len(b) < len(a) and a[:len(b)] == b:
        return "older"
    require(len(b) > len(a) and b[:len(a)] == a, "Divergent history; prefer-newer refuses to overwrite it. Use keep-both")
    require(old["sourceFormat"] == new["sourceFormat"], "Cannot compare histories from different source formats; use keep-both")
    require(new["latestMessageAt"] > old["latestMessageAt"], "Incoming history extends the archive but has no newer original message timestamp")
    require(keyed_native_digest(existing) == old["nativeDigest"], "Existing archive content was edited in T3; use keep-both")
    # Old assets must have the same bytes, even when original files changed on disk.
    old_assets = {(a["owner"], a["index"]): a["sha256"] for a in old["assets"]}
    new_assets = {(a["owner"], a["index"]): a["sha256"] for a in new["assets"]}
    require(all(new_assets.get(k) == v for k, v in old_assets.items()), "Attachment bytes changed; use keep-both")
    require(not existing["thread"].get("deletedAt"), "Cannot update a deleted archive")
    for key in ("messages", "items", "runs"):
        require({r["id"] for r in existing[key]} <= {r["id"] for r in incoming[key]},
                "Incoming representation removes archived records; use keep-both")
    return "replace"


def clone_snapshot(bundle, assets, attachment_root):
    """Retain a readable earlier version before replacing the canonical archive."""
    snapshot = deepcopy(bundle)
    old_sid = snapshot["thread"]["id"]
    sid = alternate_id(snapshot["archive"]["originalSessionId"], snapshot["archive"]["sourceFingerprint"])
    mapping = {old_sid: sid}
    for collection in ("runs", "messages", "items"):
        mapping.update({r["id"]: sid + "-" + digest(r["id"])[:32] for r in snapshot[collection]})
    for record in [snapshot["thread"], *snapshot["runs"], *snapshot["messages"], *snapshot["items"]]:
        for key in ("id", "threadId", "runId", "messageId", "userMessageId", "parentItemId"):
            if record.get(key) in mapping:
                record[key] = mapping[record[key]]
    lineage = snapshot["thread"]["lineage"]
    if lineage["rootThreadId"] == old_sid:
        lineage["rootThreadId"] = sid
    snapshot["thread"]["archivedAt"] = snapshot["thread"]["updatedAt"]
    attachments = {}
    records = []
    for a in snapshot["archive"]["assets"]:
        path = Path(attachment_root) / a["relativePath"]
        copied = assets.copy(sid, a["owner"], a["index"], {"url": str(path.resolve()), "name": a["attachment"]["name"], "mime": a["attachment"]["mimeType"]})
        require(assets.records[-1]["sha256"] == a["sha256"], "Saved attachment bytes are missing or changed")
        attachments[a["attachment"]["id"]] = copied
        records.append({**assets.records[-1], "originalReference": a["originalReference"]})
    def rewrite(value):
        if isinstance(value, dict):
            if value.get("id") in attachments and value.get("type") in ("image", "file"):
                return attachments[value["id"]]
            return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value
    for msg in snapshot["messages"]:
        msg["attachments"] = rewrite(msg["attachments"])
    for item in snapshot["items"]:
        if item.get("toolName") != "opencode_archive_source":
            if "attachments" in item:
                item["attachments"] = rewrite(item["attachments"])
            if "output" in item:
                item["output"] = rewrite(item["output"])
    archive = snapshot["archive"]
    archive["assets"] = records
    for ref in archive["references"].values():
        for key in ("threadId", "messageId", "itemId"):
            if ref.get(key) in mapping:
                ref[key] = mapping[ref[key]]
    # deepcopy preserves shared object identity only for freshly built bundles.
    for item in snapshot["items"]:
        if item.get("toolName") == "opencode_archive_source":
            item["output"] = archive
    stamp_digest(snapshot)
    refresh(snapshot)
    stamp_digest(snapshot)
    return snapshot
