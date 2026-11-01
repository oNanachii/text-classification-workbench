"""Compare tagged and current code without changing formal data or scoring test rows."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE_TAG = "v2.0-final-eval"
VALIDATION_COUNT = 783
SCORE_TOLERANCE = 1e-9
AUTHORED_SMS = [
    "Please bring the blue notebook to our project meeting tomorrow afternoon.",
    "Congratulations you won a free cash prize, claim your exclusive offer now!",
    "The appointment is confirmed for Friday, call me when you arrive.",
]
PUBLIC_URL = "https://textlab.madokanun.top"


def digest(content: bytes) -> str:
    """Hash bytes so comparisons can identify inputs without disclosing messages."""
    return hashlib.sha256(content).hexdigest()


def canonical(value: Any) -> bytes:
    """Serialize comparison evidence consistently for reproducible digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def inventory(root: Path) -> dict[str, Any]:
    """Record content and metadata to detect any change to the formal workspace."""
    result = {}
    for path in sorted([root, *root.rglob("*")]):
        stat = path.lstat()
        entry = {"mode": stat.st_mode, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        # Credentials are outside the comparison's scope; never read their contents.
        if path.is_file() and "token" not in path.name and not path.is_symlink():
            entry["sha256"] = digest(path.read_bytes())
        result[str(path.relative_to(root))] = entry
    return result


def protect_formal_data(root: Path) -> None:
    """Reject filesystem mutations under var even if future verification code regresses."""
    mutations = {
        "os.chmod",
        "os.chown",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }

    def inside(value: Any) -> bool:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(value)).resolve().is_relative_to(root)

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open" and inside(args[0]):
            flags = args[2] or 0
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags:
                raise PermissionError("Equivalence verification forbids writes under var/")
        if event in mutations and any(inside(value) for value in args[:2]):
            raise PermissionError("Equivalence verification forbids mutations under var/")

    sys.addaudithook(audit)


def worker(source: Path, layout: str) -> dict[str, Any]:
    """Score validation only and rebuild raw-data splits in an isolated temporary root."""
    import numpy as np

    formal = (PROJECT / "var").resolve()
    before = inventory(formal)
    protect_formal_data(formal)
    sys.path.insert(0, str(source))
    prefix = "textlab." if layout == "package" else ""
    data = importlib.import_module(prefix + "data")
    flow = importlib.import_module(prefix + "dataflow")
    model_module = importlib.import_module(prefix + "safe_model")
    app_module = importlib.import_module(prefix + "app_v2")
    data.DATA = PROJECT / "data"
    dataset = json.loads((formal / "dataset.json").read_text())
    validation = dataset["validation"]
    assert len(validation) == VALIDATION_COUNT
    texts = [row["text"] for row in validation]
    scores = {}
    authored = {}
    for directory in sorted((formal / "runs").iterdir()):
        if not (directory / "model.json").is_file():
            continue
        for kind in ("lr", "mlp16"):
            model = model_module.SafeModel(directory, kind)
            values = model.scores(texts)
            assert values.shape == (VALIDATION_COUNT,) and np.isfinite(values).all()
            key = directory.name + "/" + kind
            scores[key] = values.tolist()
            authored[key] = [model.predict(text, 0.5) for text in AUTHORED_SMS]
    # Only dataset.json's validation rows reach a model; the held-out file is never parsed.
    with tempfile.TemporaryDirectory(prefix="textlab-split-equivalence-") as temporary:
        store = flow.DataStore(Path(temporary) / "repartition")
        manifest = store.prepare(PROJECT / "data/raw/SMSSpamCollection")
        rebuilt = json.loads((store.root / "split-index.json").read_text())
        existing = json.loads((formal / "split-index.json").read_text())
        rebuilt_index = {row["id"]: row["split"] for row in rebuilt["index"]}
        existing_index = {row["id"]: row["split"] for row in existing["index"]}
        assert rebuilt_index == existing_index, "Repartition differs from the formal split"
        assert manifest == dataset["manifest"], "Repartition changes the dataset manifest"
        schema = app_module.create_app(root=temporary, public=True).openapi()
    after = inventory(formal)
    assert before == after, "Formal workspace contents or metadata changed"
    return {
        "validation_ids": [row["id"] for row in validation],
        "validation_sha256": digest(canonical(validation)),
        "raw_sha256": digest((PROJECT / "data/raw/SMSSpamCollection").read_bytes()),
        "scores": scores,
        "authored_predictions": authored,
        "split_membership": rebuilt_index,
        "manifest": manifest,
        "formal_inventory_sha256": digest(canonical(before)),
        "formal_files_and_metadata_unchanged": before == after,
        "openapi_sha256": digest(canonical(schema)),
    }


def public_predictions() -> dict[str, Any]:
    """Send three authored local-only requests to check deployed numerical behavior."""
    import httpx

    with httpx.Client(base_url=PUBLIC_URL, timeout=30, trust_env=False) as client:
        health = client.get("/api/health").raise_for_status().json()
        predictions = [
            client.post("/api/predict", json={"text": text}).raise_for_status().json()
            for text in AUTHORED_SMS
        ]
    return {
        "url": PUBLIC_URL,
        "texts": AUTHORED_SMS,
        "health": health,
        "predictions": predictions,
        "external_model_called": False,
    }


def run_worker(source: Path, layout: str) -> dict[str, Any]:
    """Use a fresh interpreter so tagged and refactored modules cannot share imports."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--worker",
            str(source),
            "--layout",
            layout,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return json.loads(result.stdout)


def tagged_worker() -> dict[str, Any]:
    """Extract the preserved commit to a temporary directory without touching Git state."""
    with tempfile.TemporaryDirectory(prefix="textlab-tag-equivalence-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "archive", EVIDENCE_TAG], cwd=PROJECT, stdout=stream, check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(root / "source", filter="data")
        return run_worker(root / "source", "flat")


def main() -> None:
    """Capture pre-change evidence or verify scores, split identity and deployed behavior."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-before", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--check-live", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=PROJECT / "artifacts/refactor-equivalence.json"
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--layout", choices=["flat", "package"], default="package", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.layout)))
        return
    if args.capture_before:
        baseline = tagged_worker()
        baseline["live"] = public_predictions()
        args.capture_before.write_text(json.dumps(baseline, indent=2) + "\n")
        print("Captured tagged validation/split evidence and three live authored predictions")
        return
    baseline = json.loads(args.before.read_text()) if args.before else tagged_worker()
    current = run_worker(PROJECT, "package")
    assert baseline["validation_ids"] == current["validation_ids"]
    assert baseline["validation_sha256"] == current["validation_sha256"]
    assert baseline["raw_sha256"] == current["raw_sha256"]
    assert baseline["split_membership"] == current["split_membership"]
    assert baseline["manifest"] == current["manifest"]
    assert baseline["openapi_sha256"] == current["openapi_sha256"]
    assert baseline["formal_inventory_sha256"] == current["formal_inventory_sha256"]
    assert baseline["scores"].keys() == current["scores"].keys()
    comparisons = {}
    for key, values in baseline["scores"].items():
        differences = [abs(a - b) for a, b in zip(values, current["scores"][key], strict=True)]
        maximum = max(differences)
        assert maximum < SCORE_TOLERANCE, (key, maximum)
        comparisons[key] = {
            "rows_compared": len(values),
            "max_absolute_difference": maximum,
            "before_scores_sha256": digest(canonical(values)),
            "after_scores_sha256": digest(canonical(current["scores"][key])),
        }
    live = {"status": "not_requested"}
    if args.check_live:
        assert "live" in baseline, "Capture the pre-deployment live predictions first"
        deployed = public_predictions()
        assert baseline["live"]["texts"] == deployed["texts"]
        assert baseline["live"]["health"] == deployed["health"]
        assert baseline["live"]["predictions"] == deployed["predictions"]
        selected = deployed["health"]["loaded"]
        key = selected["run_id"] + "/" + selected["kind"]
        for old, prediction in zip(
            current["authored_predictions"][key], deployed["predictions"], strict=True
        ):
            assert old["spam_score"] == prediction["spam_score"]
            assert old["label"] == prediction["label"]
            assert old["threshold"] == prediction["threshold"]
        live = {
            "status": "passed",
            "before": baseline["live"],
            "after": deployed,
            "max_absolute_difference": 0.0,
        }
    evidence_commit = subprocess.check_output(
        ["git", "rev-parse", EVIDENCE_TAG], cwd=PROJECT, text=True
    ).strip()
    dependencies = ("evaluation.py", "safe_model.py", "data.py", "experiments.py", "dataflow.py")
    tagged_hashes = {
        name: digest(
            subprocess.check_output(["git", "show", EVIDENCE_TAG + ":" + name], cwd=PROJECT)
        )
        for name in dependencies
    }
    final_plan = json.loads((PROJECT / "var/final-plan.json").read_text())
    assert tagged_hashes == final_plan["source_sha256"], "Tag differs from final-evaluation plan"
    source_hashes = {
        str(path.relative_to(PROJECT)): digest(path.read_bytes())
        for path in sorted(
            [*PROJECT.glob("textlab/*.py"), *PROJECT.glob("legacy/*.py"), Path(__file__).resolve()]
        )
    }
    import numpy as np

    report = {
        "status": "passed",
        "evidence_tag": EVIDENCE_TAG,
        "evidence_commit": evidence_commit,
        "verification_script": "scripts/verify_refactor.py",
        "current_source_sha256": source_hashes,
        "runtime": {"python": list(sys.version_info[:3]), "numpy": np.__version__},
        "final_plan_dependencies_match_tag": True,
        "tagged_dependency_sha256": tagged_hashes,
        "openapi": {"unchanged": True, "sha256": current["openapi_sha256"]},
        "tolerance_strictly_less_than": SCORE_TOLERANCE,
        "validation": comparisons,
        "validation_sha256": current["validation_sha256"],
        "split": {
            "sample_count": len(current["split_membership"]),
            "changed_assignments": 0,
            "membership_sha256": digest(canonical(current["split_membership"])),
            "raw_sha256": current["raw_sha256"],
            "manifest": current["manifest"],
        },
        "formal_data": {
            "access": "read-only; mutation audit guard enabled in workers",
            "contents_and_metadata_unchanged": True,
            "inventory_sha256": current["formal_inventory_sha256"],
        },
        "heldout_test": {
            "model_inference_rows": 0,
            "evaluated": False,
            "usage": "Raw repartition and ID membership only; no model inputs",
        },
        "live": live,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(args.output),
                "models": len(comparisons),
                "validation_rows": VALIDATION_COUNT,
                "changed_split_assignments": 0,
                "live": live["status"],
            }
        )
    )


if __name__ == "__main__":
    main()
