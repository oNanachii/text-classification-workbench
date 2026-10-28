"""Editable training-sample copies; source files and frozen splits stay untouched."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _label(value):
    if value not in ("ham", "spam"):
        raise ValueError("label must be ham or spam")
    return value


class SampleStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self, *, create=False):
        if not create and not self.path.is_file():
            raise RuntimeError("Sample store is not initialized")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, training_rows, split_sha256) -> dict:
        if not isinstance(split_sha256, str) or not split_sha256:
            raise ValueError("split_sha256 must be a nonempty string")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        expected = {"schema_version": "1", "split_sha256": split_sha256}
        with self._connection(create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            created = not tables and not existed
            if created:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute("""
                    CREATE TABLE samples (
                        id TEXT PRIMARY KEY,
                        text TEXT NOT NULL,
                        original_label TEXT NOT NULL CHECK(original_label IN ('ham', 'spam')),
                        label TEXT NOT NULL CHECK(label IN ('ham', 'spam')),
                        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                        updated_at TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE corrections (
                        id INTEGER PRIMARY KEY,
                        sample_id TEXT NOT NULL REFERENCES samples(id),
                        old_label TEXT NOT NULL CHECK(old_label IN ('ham', 'spam')),
                        new_label TEXT NOT NULL CHECK(new_label IN ('ham', 'spam')),
                        note TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        created_at TEXT NOT NULL,
                        UNIQUE(sample_id, revision)
                    )
                """)
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)", expected.items()
                )
                now = _timestamp()
                for row in training_rows:
                    if not isinstance(row["id"], str) or not row["id"]:
                        raise ValueError("sample id must be a nonempty string")
                    if not isinstance(row["text"], str) or not row["text"].strip():
                        raise ValueError("sample text must be a nonempty string")
                    label = _label(row["label"])
                    connection.execute(
                        "INSERT INTO samples "
                        "(id, text, original_label, label, revision, updated_at) "
                        "VALUES (?, ?, ?, ?, 0, ?)",
                        (row["id"], row["text"], label, label, now),
                    )
            else:
                if tables != {"metadata", "samples", "corrections"}:
                    raise ValueError("Existing sample store has an incompatible schema")
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if metadata != expected:
                    raise ValueError("Existing sample store metadata or split hash differs")
                # Check all public columns before accepting an existing database.
                connection.execute("SELECT id, text, original_label, label, revision, updated_at FROM samples LIMIT 0")
                connection.execute("SELECT id, sample_id, old_label, new_label, note, revision, created_at FROM corrections LIMIT 0")
            count = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        return {"schema_version": 1, "split_sha256": split_sha256,
                "sample_count": count, "created": created}

    def list_samples(self, *, limit=20, offset=0, label=None, query="") -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if label is not None:
            _label(label)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        clause = "WHERE (? IS NULL OR label = ?) AND instr(text, ?) > 0"
        parameters = (label, label, query)
        with self._connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute(
                "SELECT COUNT(*) FROM samples " + clause, parameters
            ).fetchone()[0]
            items = [dict(row) for row in connection.execute(
                "SELECT * FROM samples " + clause + " ORDER BY id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_sample(self, sample_id) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM samples WHERE id = ?", (sample_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def correct_label(self, sample_id, label, note="") -> dict | None:
        _label(label)
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM samples WHERE id = ?", (sample_id,)
            ).fetchone()
            if row is None:
                return None
            sample = dict(row)
            if sample["label"] == label:
                return {"sample": sample, "changed": False, "correction_id": None}
            revision, now = sample["revision"] + 1, _timestamp()
            connection.execute(
                "UPDATE samples SET label = ?, revision = ?, updated_at = ? WHERE id = ?",
                (label, revision, now, sample_id),
            )
            cursor = connection.execute(
                "INSERT INTO corrections "
                "(sample_id, old_label, new_label, note, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sample_id, sample["label"], label, note, revision, now),
            )
            sample.update(label=label, revision=revision, updated_at=now)
            return {"sample": sample, "changed": True, "correction_id": cursor.lastrowid}

    def correction_history(self, sample_id) -> list[dict]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM corrections WHERE sample_id = ? ORDER BY id", (sample_id,)
            )]
