"""Source-preserving SMS preparation; this module never evaluates a model."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SOURCE_PAGE = "https://archive.ics.uci.edu/dataset/228/sms+spam+collection"
SOURCE_URL = "https://archive.ics.uci.edu/static/public/228/sms+spam+collection.zip"
SEED = 42
V1_REMAINING_RATIO = 0.30
V1_REMAINING_TEST_RATIO = 0.50
DOWNLOAD_TIMEOUT_SECONDS = 60


def digest(value: bytes) -> str:
    """Hash source bytes so dataset provenance can be checked."""
    return hashlib.sha256(value).hexdigest()


def save_json(path: Path, value: Any) -> None:
    """Write readable JSON so source and split manifests remain inspectable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize(text: str) -> str:
    """Normalize case and whitespace to give duplicate detection a stable key."""
    return " ".join(text.casefold().split())


def download() -> None:
    """Preserve the pinned archive and source files for reproducible preparation."""
    raw = DATA / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    archive = raw / "sms_spam_collection.zip"
    if not archive.exists():
        with urlopen(SOURCE_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content = response.read()
        # Check both known files before saving anything; do not extract arbitrary paths.
        with ZipFile(io.BytesIO(content)) as zipped:
            for name in ("SMSSpamCollection", "readme"):
                zipped.read(name)
        archive.write_bytes(content)
    with ZipFile(archive) as zipped:
        for name in ("SMSSpamCollection", "readme"):
            target = raw / name
            content = zipped.read(name)
            if target.exists() and target.read_bytes() != content:
                raise ValueError(f"Existing source differs from archive: {target}")
            if not target.exists():
                target.write_bytes(content)
    save_json(
        DATA / "source.json",
        {
            "dataset": "SMS Spam Collection",
            "attribution": "Almeida, T. & Hidalgo, J. (2011). UCI. DOI: 10.24432/C5CC84",
            "source_page": SOURCE_PAGE,
            "download_url": SOURCE_URL,
            "license_listed_by_uci": "CC BY 4.0",
            "archive_sha256": digest(archive.read_bytes()),
            "raw_sha256": digest((raw / "SMSSpamCollection").read_bytes()),
        },
    )


def clean(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """Deduplicate normalized text and quarantine conflicts to avoid ambiguous labels."""
    groups = defaultdict(list)
    empty = []
    for record in records:
        key = normalize(record["raw_text"])
        if not key:
            empty.append(record)
        else:
            groups[key].append(record)
    accepted, conflicts = [], []
    duplicates_removed = 0
    for key, members in sorted(groups.items()):
        labels = {row["label"] for row in members}
        if len(labels) > 1:
            conflicts.append({"text": key, "records": members})
        else:
            accepted.append(
                {
                    "id": digest(key.encode("utf-8")),
                    "text": key,
                    "label": next(iter(labels)),
                    "source_lines": sorted(row["source_line"] for row in members),
                }
            )
            duplicates_removed += len(members) - 1
    return accepted, conflicts, empty, duplicates_removed


def prepare() -> dict[str, Any]:
    """Create the frozen v1 stratified splits without replacing a different dataset."""
    from sklearn.model_selection import train_test_split

    raw = DATA / "raw" / "SMSSpamCollection"
    records = []
    for number, line in enumerate(raw.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"Unlabeled blank source line: {number}")
        label, text = line.split("\t", 1)
        if label not in {"ham", "spam"}:
            raise ValueError(f"Unknown label at source line {number}: {label!r}")
        records.append({"source_line": number, "raw_text": text, "label": label})
    accepted, conflicts, empty, duplicates = clean(records)
    train, remaining = train_test_split(
        accepted,
        test_size=V1_REMAINING_RATIO,
        random_state=SEED,
        stratify=[row["label"] for row in accepted],
    )
    validation, test = train_test_split(
        remaining,
        test_size=V1_REMAINING_TEST_RATIO,
        random_state=SEED,
        stratify=[row["label"] for row in remaining],
    )
    splits = {"train": train, "validation": validation, "test": test}
    for first, second in (("train", "validation"), ("train", "test"), ("validation", "test")):
        if {row["id"] for row in splits[first]} & {row["id"] for row in splits[second]}:
            raise AssertionError(f"Repeated normalized text across {first}/{second}")
    manifest = {
        "seed": SEED,
        "raw_sha256": digest(raw.read_bytes()),
        "normalization": "casefold + collapse whitespace + trim",
        "conflict_policy": "quarantine all records sharing normalized text but different labels",
        "near_duplicate_check": "not performed; paraphrases/templates may cross splits",
        "raw_rows": len(records),
        "accepted_unique_rows": len(accepted),
        "duplicate_rows_removed": duplicates,
        "empty_rows_removed": len(empty),
        "conflict_groups": len(conflicts),
        "conflict_rows": sum(len(group["records"]) for group in conflicts),
        "counts": {
            name: dict(Counter(row["label"] for row in rows)) for name, rows in splits.items()
        },
    }
    result = {"manifest": manifest, "splits": splits}
    target = DATA / "splits.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != result:
        raise ValueError(
            "Frozen splits already exist and differ. Create a new data version explicitly."
        )
    save_json(target, result)
    save_json(DATA / "quarantine.json", {"conflicts": conflicts, "empty": empty})
    return manifest


def load_splits() -> tuple[dict[str, Any], str]:
    """Load v1 splits and verify the raw source so provenance cannot drift."""
    path = DATA / "splits.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        digest((DATA / "raw" / "SMSSpamCollection").read_bytes())
        != result["manifest"]["raw_sha256"]
    ):
        raise ValueError("Raw source changed after data preparation")
    return result, digest(path.read_bytes())


def main() -> None:
    """Download the pinned source explicitly for both current and legacy workflows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["download"])
    parser.parse_args()
    download()


if __name__ == "__main__":
    main()
