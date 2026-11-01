"""Verify training snapshot boundaries with disposable SQLite; no training or test evaluation."""

from typing import Any

import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from textlab import data
from legacy.storage import SampleStore
from legacy import training_snapshot as snapshots


def row(text: str, label: str) -> Any:
    """Build a synthetic SMS row for isolated checks without real-data access."""
    return {
        "id": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "label": label,
        "source_lines": [1],
    }


class SnapshotChecks(unittest.TestCase):
    def setUp(self) -> None:
        """Prepare isolated synthetic fixtures for this storage check."""
        self.temporary = TemporaryDirectory(prefix="sms-training-snapshot-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw = self.root / "source.txt"
        self.raw.write_bytes(b"Synthetic training snapshot fixture\n")
        self.rows = [row("first ham message", "ham"), row("second spam message", "spam")]
        manifest = {
            "seed": 42,
            "raw_sha256": snapshots.file_sha(self.raw),
            "normalization": "casefold + collapse whitespace + trim",
            "counts": {"train": {"ham": 1, "spam": 1}, "validation": {}, "test": {}},
        }
        self.split = self.root / "splits.json"
        # Export consumes only manifest/train. Held-out data is never selected for output.
        data.save_json(
            self.split,
            {"manifest": manifest, "splits": {"train": self.rows, "validation": [], "test": []}},
        )
        self.database = self.root / "working.sqlite3"
        self.store = SampleStore(self.database)
        self.store.initialize(self.rows, snapshots.file_sha(self.split))
        self.output = self.root / "exports"

    def build(self) -> Any:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        return snapshots.build_snapshot(self.database, self.split, self.raw)

    def export(self) -> Any:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        return snapshots.export_snapshot(
            self.database, self.output, split_path=self.split, raw_path=self.raw
        )

    def execute(self, sql: Any, parameters: Any = ()) -> Any:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        with sqlite3.connect(self.database) as connection:
            connection.execute(sql, parameters)

    def test_identical_export_reuses_version_and_correction_creates_new_version(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        first = self.export()
        old_path = Path(first["path"])
        old_bytes = old_path.read_bytes()
        again = self.export()
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["snapshot_id"], again["snapshot_id"])
        self.store.correct_label(self.rows[0]["id"], "spam", "Synthetic review correction")
        changed = self.export()
        self.assertNotEqual(first["snapshot_id"], changed["snapshot_id"])
        self.assertEqual(changed["correction_count"], 1)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        content = json.loads(Path(changed["path"]).read_text())["content"]
        self.assertEqual(
            {sample["id"] for sample in content["samples"]}, {r["id"] for r in self.rows}
        )
        self.assertEqual(content["split"], "train")
        self.assertEqual(content["source"]["frozen_split_sha256"], snapshots.file_sha(self.split))

    def test_non_training_row_and_changed_source_text_are_rejected(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        self.execute(
            "INSERT INTO samples VALUES (?, ?, ?, ?, 0, ?)",
            ("outside-train", "unapproved sample", "ham", "ham", "fixture-time"),
        )
        with self.assertRaisesRegex(ValueError, "original train IDs"):
            self.export()
        self.assertFalse(self.output.exists())
        self.execute("DELETE FROM samples WHERE id = ?", ("outside-train",))
        self.execute(
            "UPDATE samples SET text = ? WHERE id = ?", ("changed text", self.rows[0]["id"])
        )
        with self.assertRaisesRegex(ValueError, "source or revision"):
            self.build()

    def test_failed_history_insert_rolls_back_label_and_snapshot_stays_identical(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        before = self.build()
        before_state = snapshots.read_working_state(self.database)
        self.execute(
            "CREATE TRIGGER reject_history BEFORE INSERT ON corrections "
            "BEGIN SELECT RAISE(ABORT, 'injected history failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.correct_label(self.rows[0]["id"], "spam", "must roll back")
        self.assertEqual(snapshots.read_working_state(self.database), before_state)
        self.assertEqual(self.build(), before)

    def test_missing_history_and_mismatched_database_provenance_are_rejected(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        self.store.correct_label(self.rows[0]["id"], "spam", "fixture")
        self.execute("DELETE FROM corrections")
        with self.assertRaisesRegex(ValueError, "sample revision"):
            self.build()
        self.execute("UPDATE metadata SET value = ? WHERE key = 'split_sha256'", ("wrong-version",))
        with self.assertRaisesRegex(ValueError, "frozen split version"):
            self.build()

    def test_corrupted_existing_destination_is_preserved(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        first = self.export()
        target = Path(first["path"])
        target.write_bytes(b"corrupt existing snapshot")
        with self.assertRaises(snapshots.SnapshotConflictError):
            self.export()
        self.assertEqual(target.read_bytes(), b"corrupt existing snapshot")
        self.assertFalse(list(self.output.glob("*.tmp")))

    def test_publish_failure_leaves_no_partial_snapshot(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        with patch.object(
            snapshots.os, "link", side_effect=PermissionError("injected publish failure")
        ):
            with self.assertRaises(PermissionError):
                self.export()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_database_is_not_created(self) -> None:
        """Verify an isolated boundary with synthetic fixtures to preserve v1 behavior."""
        absent = self.root / "absent.sqlite3"
        with self.assertRaises(FileNotFoundError):
            snapshots.read_working_state(absent)
        self.assertFalse(absent.exists())


def verify_real_read_only_export() -> Any:
    """Check a read-only export of the existing training source and record provenance."""
    database = data.DATA / "working.sqlite3"
    protected = [database, data.DATA / "splits.json", data.DATA / "raw" / "SMSSpamCollection"]
    protected.extend(path for path in (data.ROOT / "artifacts").rglob("*") if path.is_file())
    before_hashes = {str(path): snapshots.file_sha(path) for path in protected}
    before_state = snapshots.read_working_state(database)
    with TemporaryDirectory(prefix="sms-real-training-export-") as temporary:
        first = snapshots.export_snapshot(database, Path(temporary))
        again = snapshots.export_snapshot(database, Path(temporary))
        if not first["created"] or again["created"] or first["snapshot_id"] != again["snapshot_id"]:
            raise AssertionError("Real source export was not stable")
        payload = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
        if payload["content"]["sample_count"] != len(before_state[1]):
            raise AssertionError("Real sample count changed")
    if snapshots.read_working_state(database) != before_state:
        raise AssertionError("Real labels or correction history changed")
    if {str(path): snapshots.file_sha(path) for path in protected} != before_hashes:
        raise AssertionError("Protected source, model or report bytes changed")
    return {
        "snapshot_id": first["snapshot_id"],
        "sample_count": first["sample_count"],
        "correction_count": first["correction_count"],
        "labels_and_history_unchanged": True,
        "protected_file_count": len(protected),
        "protected_files_unchanged": True,
        "output_scope": "temporary directory, removed after verification",
    }


def main() -> None:
    """Run the explicit v1 verification entry point and preserve its report."""
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SnapshotChecks)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    real = verify_real_read_only_export()
    print(
        json.dumps(
            {
                "status": "passed",
                "isolated_test_count": result.testsRun,
                "real_export": real,
                "training_performed": False,
                "held_out_text_parsed": False,
                "test_set_evaluated": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
