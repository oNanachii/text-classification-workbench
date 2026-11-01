"""Export immutable training-only snapshots; no model training or held-out text parsing."""

from typing import Any

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from textlab import data


HASH_CHUNK_BYTES = 65536
SQLITE_TIMEOUT_SECONDS = 10


class SnapshotConflictError(ValueError):
    """A content-addressed destination already exists with different bytes."""


def canonical(value: Any) -> bytes:
    """Encode stable JSON bytes so content-addressed snapshots remain reproducible."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def file_sha(path: Path | str) -> str:
    """Hash a file in bounded chunks to record its exact source bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _character(stream):
    while True:
        char = stream.read(1)
        if not char or not char.isspace():
            return char


def _expect(stream, expected):
    if _character(stream) != expected:
        raise ValueError("Unexpected frozen split format; expected the data.py schema")


def _value(stream):
    """Consume one string/object/array without reading subsequent JSON values."""
    first = _character(stream)
    if first not in ('"', "{", "["):
        raise ValueError("Unexpected frozen split value")
    chars, depth, quoted, escaped = [first], int(first in "{["), first == '"', False
    while quoted or depth:
        char = stream.read(1)
        if not char:
            raise ValueError("Incomplete frozen split value")
        chars.append(char)
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return json.loads("".join(chars))


def read_training_source(
    split_path: Path | str, raw_path: Path | str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read data.py's ordered manifest/train prefix, never parse held-out arrays.

    The entire frozen file is hashed as opaque bytes for provenance. Reordered or
    incompatible schemas fail rather than falling back to loading all splits.
    """
    split_path = Path(split_path)
    split_sha = file_sha(split_path)
    with split_path.open("r", encoding="utf-8") as stream:
        _expect(stream, "{")
        if _value(stream) != "manifest":
            raise ValueError("Expected manifest before splits")
        _expect(stream, ":")
        manifest = _value(stream)
        _expect(stream, ",")
        if _value(stream) != "splits":
            raise ValueError("Expected splits after manifest")
        _expect(stream, ":")
        _expect(stream, "{")
        if _value(stream) != "train":
            raise ValueError("Expected train first; held-out arrays are not parsed")
        _expect(stream, ":")
        train = _value(stream)
    if file_sha(split_path) != split_sha:
        raise ValueError("Frozen split file changed while reading")
    if not isinstance(manifest, dict) or not isinstance(train, list) or not train:
        raise ValueError("Invalid training source")
    if file_sha(raw_path) != manifest.get("raw_sha256"):
        raise ValueError("Raw source differs from frozen manifest")
    source = {}
    for row in train:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("text"), str)
            or not row["text"]
            or row.get("label") not in ("ham", "spam")
            or row.get("id") != hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            or data.normalize(row["text"]) != row["text"]
            or row["id"] in source
        ):
            raise ValueError("Invalid or repeated source training row")
        source[row["id"]] = row
    if dict(Counter(row["label"] for row in train)) != manifest.get("counts", {}).get("train"):
        raise ValueError("Training rows do not match manifest counts")
    return {
        "frozen_split_sha256": split_sha,
        "raw_sha256": manifest["raw_sha256"],
        "manifest": manifest,
        "source_train_sha256": hashlib.sha256(
            canonical([source[k] for k in sorted(source)])
        ).hexdigest(),
    }, source


def read_working_state(
    database_path: Path | str,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read metadata, samples and correction history from one read-only transaction."""
    path = Path(database_path).resolve(strict=True)
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        samples = [
            dict(row)
            for row in connection.execute(
                "SELECT id, text, original_label, label, revision, updated_at "
                "FROM samples ORDER BY id"
            )
        ]
        history = [
            dict(row)
            for row in connection.execute(
                "SELECT id, sample_id, old_label, new_label, note, revision, created_at "
                "FROM corrections ORDER BY sample_id, revision"
            )
        ]
        return metadata, samples, history
    finally:
        connection.close()


def build_snapshot(
    database_path: Path | str, split_path: Path | str, raw_path: Path | str
) -> dict[str, Any]:
    """Validate the working revision chain and build an immutable training snapshot."""
    provenance, source = read_training_source(split_path, raw_path)
    metadata, samples, history = read_working_state(database_path)
    if metadata != {"schema_version": "1", "split_sha256": provenance["frozen_split_sha256"]}:
        raise ValueError("Working database does not match the frozen split version")
    if len(samples) != len(source) or {row["id"] for row in samples} != set(source):
        raise ValueError("Working samples must contain exactly the original train IDs")
    by_id = {row["id"]: row for row in samples}
    chains = {key: [] for key in source}
    seen_history_ids = set()
    for record in history:
        if record["sample_id"] not in chains:
            raise ValueError("Correction references a non-training sample")
        if type(record["id"]) is not int or record["id"] < 1 or record["id"] in seen_history_ids:
            raise ValueError("Invalid correction identity")
        seen_history_ids.add(record["id"])
        chains[record["sample_id"]].append(record)
    for identity, sample in by_id.items():
        original = source[identity]
        if (
            sample["text"] != original["text"]
            or sample["original_label"] != original["label"]
            or type(sample["revision"]) is not int
            or sample["revision"] < 0
            or not isinstance(sample["updated_at"], str)
            or not sample["updated_at"]
        ):
            raise ValueError("Working training source or revision was modified")
        label = original["label"]
        chain = chains[identity]
        if len(chain) != sample["revision"]:
            raise ValueError("Correction history does not match sample revision")
        for revision, record in enumerate(chain, 1):
            if (
                record["revision"] != revision
                or record["old_label"] != label
                or record["new_label"] not in ("ham", "spam")
                or record["new_label"] == label
                or not isinstance(record["note"], str)
                or not isinstance(record["created_at"], str)
                or not record["created_at"]
            ):
                raise ValueError("Invalid correction history chain")
            label = record["new_label"]
        if sample["label"] != label or (chain and sample["updated_at"] != chain[-1]["created_at"]):
            raise ValueError("Current label or timestamp differs from correction history")
    # No export timestamp or output path: identical source state has an identical ID.
    content = {
        "source": provenance,
        "split": "train",
        "label_mapping": {"ham": 0, "spam": 1},
        "sample_count": len(samples),
        "class_counts": dict(Counter(row["label"] for row in samples)),
        "samples": samples,
        "label_history": history,
    }
    digest = hashlib.sha256(canonical(content)).hexdigest()
    return {
        "schema_version": 1,
        "snapshot_id": "train-" + digest,
        "content_sha256": digest,
        "content": content,
    }


def publish_snapshot(snapshot: dict[str, Any], output_directory: Path | str) -> dict[str, Any]:
    """Publish a complete JSON file atomically, without replacing an existing path."""
    digest = hashlib.sha256(canonical(snapshot["content"])).hexdigest()
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("content_sha256") != digest
        or snapshot.get("snapshot_id") != "train-" + digest
    ):
        raise ValueError("Snapshot identity does not match content")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    target = output / (snapshot["snapshot_id"] + ".json")
    payload = canonical(snapshot) + b"\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".training-snapshot-", suffix=".tmp", dir=output, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            created = True
        except FileExistsError:
            # O_NOFOLLOW also prevents a pre-existing symlink from redirecting reads.
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if (
                    not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                    or stream.read(len(payload) + 1) != payload
                ):
                    raise SnapshotConflictError("Existing snapshot differs; it was not overwritten")
            created = False
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "path": str(target.resolve()),
            "created": created,
            "sample_count": snapshot["content"]["sample_count"],
            "correction_count": len(snapshot["content"]["label_history"]),
            "training_performed": False,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_snapshot(
    database_path: Path | str,
    output_directory: Path | str,
    *,
    split_path: Path | str = data.DATA / "splits.json",
    raw_path: Path | str = data.DATA / "raw" / "SMSSpamCollection",
) -> dict[str, Any]:
    """Export a validated training snapshot without modifying the source database."""
    return publish_snapshot(build_snapshot(database_path, split_path, raw_path), output_directory)


def main() -> None:
    """Export a snapshot only when the v1 CLI is explicitly invoked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=data.DATA / "working.sqlite3")
    parser.add_argument(
        "--output", type=Path, default=data.ROOT / "artifacts" / "training_snapshots"
    )
    arguments = parser.parse_args()
    result = export_snapshot(arguments.database, arguments.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
