"""Real offline CPU training and explicit service selection with provenance."""

from __future__ import annotations

from copy import deepcopy
import importlib.metadata
import fcntl
import os
from pathlib import Path
from typing import Any
import random
import shutil
import time
import numpy as np
from textlab.dataflow import DataStore, ID, atomic, canonical, immutable, now, read_json, sha
from textlab.safe_model import SafeModel

DEFAULT_SEED = 42
MAX_FEATURES = 2000
MLP_HIDDEN_UNITS = 16
DEFAULT_EPOCHS = 20
MAX_EPOCHS = 100
BATCH_SIZE = 64
LEARNING_RATE = 0.001
DEFAULT_THRESHOLD = 0.5
LR_MAX_ITER = 1000
CPU_THREADS = 2
ERROR_EXAMPLE_LIMIT = 12
DURATION_DECIMALS = 3
LR_RELOAD_RTOL = 1e-7
LR_RELOAD_ATOL = 1e-8
MLP_RELOAD_RTOL = 1e-5
MLP_RELOAD_ATOL = 1e-6

DEFAULT_CONFIG = {
    "seed": DEFAULT_SEED,
    "max_features": MAX_FEATURES,
    "hidden_units": MLP_HIDDEN_UNITS,
    "epochs": DEFAULT_EPOCHS,
    "batch_size": BATCH_SIZE,
    "learning_rate": LEARNING_RATE,
    "threshold": DEFAULT_THRESHOLD,
    "lr_max_iter": LR_MAX_ITER,
    "cpu_threads": CPU_THREADS,
}


def code_hash() -> str:
    """Hash the current Python sources so each training run records its implementation."""
    root = Path(__file__).resolve().parent
    return sha(
        b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in sorted(root.glob("*.py")))
    )


def metric(
    labels: list[str], scores: list[float] | np.ndarray, threshold: float = DEFAULT_THRESHOLD
) -> dict[str, int | float | None]:
    """Compute fixed binary metrics so training and evaluation use the same definitions."""
    y = np.asarray([v == "spam" for v in labels])
    p = np.asarray(scores) >= threshold
    tp = int(np.sum(y & p))
    tn = int(np.sum(~y & ~p))
    fp = int(np.sum(~y & p))
    fn = int(np.sum(y & ~p))

    def class_f1(a: int, b: int, c: int) -> float:
        """Compute one class F1 with the historical zero-denominator convention."""
        return 2 * a / (2 * a + b + c) if 2 * a + b + c else 0.0

    return {
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "macro_f1": (class_f1(tp, fp, fn) + class_f1(tn, fn, fp)) / 2,
        "accuracy": (tp + tn) / len(y),
        "count": len(y),
    }


def train(
    root: str | Path, snapshot_id: str, run_id: str, epochs: int = DEFAULT_EPOCHS
) -> dict[str, Any]:
    """Train CPU candidates from an immutable snapshot, using only validation for selection."""
    import torch
    from torch import nn
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits

    store = DataStore(root)
    snapshot = store.load_snapshot(snapshot_id)
    dataset = store.require_prepared()
    if not ID.fullmatch(run_id):
        raise ValueError("Run ID uses lowercase letters, digits, underscore or hyphen")
    if type(epochs) is not int or not 1 <= epochs <= MAX_EPOCHS:
        raise ValueError("Epochs must be 1..100")
    directory = store.root / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    config = {**DEFAULT_CONFIG, "epochs": epochs}
    started = time.monotonic()
    worker_fd = os.open(directory / ".training.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source_directory = directory / "source"
    source_directory.mkdir()
    for source in Path(__file__).resolve().parent.glob("*.py"):
        shutil.copy2(source, source_directory / source.name)
    initial = {
        "run_id": run_id,
        "state": "running",
        "snapshot_id": snapshot_id,
        "dataset_id": dataset["manifest"]["dataset_id"],
        "code_sha256": code_hash(),
        "config": config,
        "started_at": now(),
        "evaluation_split": "validation",
        "test_set_evaluated": False,
    }
    atomic(directory / "report.json", initial)
    try:
        random.seed(config["seed"])
        np.random.seed(config["seed"])
        torch.manual_seed(config["seed"])
        torch.set_num_threads(CPU_THREADS)
        threadpool_limits(limits=CPU_THREADS)
        rows = snapshot["content"]["samples"]
        validation = dataset["validation"]
        texts = [r["text"] for r in rows]
        labels = [r["label"] for r in rows]
        v = TfidfVectorizer(max_features=config["max_features"], lowercase=False)
        x = v.fit_transform(texts)
        xv = v.transform([r["text"] for r in validation])
        y = np.asarray([r["label"] == "spam" for r in rows], dtype=np.float32)
        yv = np.asarray([r["label"] == "spam" for r in validation], dtype=np.float32)
        lr = LogisticRegression(max_iter=config["lr_max_iter"], random_state=config["seed"])
        lr.fit(x, labels)
        ls = lr.predict_proba(xv)[:, list(lr.classes_).index("spam")]
        xt = torch.from_numpy(x.toarray().astype(np.float32))
        xvt = torch.from_numpy(xv.toarray().astype(np.float32))
        yt = torch.from_numpy(y)
        yvt = torch.from_numpy(yv)
        model = nn.Sequential(
            nn.Linear(x.shape[1], MLP_HIDDEN_UNITS), nn.ReLU(), nn.Linear(MLP_HIDDEN_UNITS, 1)
        )
        opt = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
        loss_fn = nn.BCEWithLogitsLoss()
        history = []
        best = None
        for epoch in range(1, epochs + 1):
            model.train()
            order = torch.randperm(len(yt))
            for j in range(0, len(order), BATCH_SIZE):
                ix = order[j : j + BATCH_SIZE]
                opt.zero_grad()
                loss = loss_fn(model(xt[ix]).squeeze(-1), yt[ix])
                loss.backward()
                opt.step()
            model.eval()
            with torch.no_grad():
                tl = float(loss_fn(model(xt).squeeze(-1), yt))
                vl = float(loss_fn(model(xvt).squeeze(-1), yvt))
            if not np.isfinite([tl, vl]).all():
                raise ValueError("Training produced nonfinite loss")
            history.append({"epoch": epoch, "training_loss": tl, "validation_loss": vl})
            if best is None or vl < best["loss"]:
                best = {"loss": vl, "epoch": epoch, "weights": deepcopy(model.state_dict())}
        model.load_state_dict(best["weights"])
        model.eval()
        with torch.no_grad():
            ms = torch.sigmoid(model(xvt).squeeze(-1)).numpy()
        vector = {
            "vocabulary": v.vocabulary_,
            "idf": v.idf_.tolist(),
            "normalization": "casefold + collapse whitespace + trim",
            "token_pattern": r"(?u)\b\w\w+\b",
            "norm": "l2",
            "fit_split": "train",
            "snapshot_id": snapshot_id,
        }
        # sklearn vocabulary values can be numpy ints.
        vector["vocabulary"] = {k: int(i) for k, i in vector["vocabulary"].items()}
        immutable(directory / "vocabulary.json", vector)
        weights = model.state_dict()
        np.savez_compressed(
            directory / "weights.npz",
            lr_coef=lr.coef_[0],
            lr_intercept=lr.intercept_,
            mlp_w1=weights["0.weight"].numpy(),
            mlp_b1=weights["0.bias"].numpy(),
            mlp_w2=weights["2.weight"].numpy(),
            mlp_b2=weights["2.bias"].numpy(),
        )
        metadata = {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "dataset_id": dataset["manifest"]["dataset_id"],
            "weights_sha256": sha((directory / "weights.npz").read_bytes()),
            "vocabulary_sha256": sha((directory / "vocabulary.json").read_bytes()),
            "feature_count": len(v.vocabulary_),
        }
        immutable(directory / "model.json", metadata)
        restored = {kind: SafeModel(directory, kind) for kind in ("lr", "mlp16")}
        np.testing.assert_allclose(
            restored["lr"].scores([r["text"] for r in validation]),
            ls,
            rtol=LR_RELOAD_RTOL,
            atol=LR_RELOAD_ATOL,
        )
        np.testing.assert_allclose(
            restored["mlp16"].scores([r["text"] for r in validation]),
            ms,
            rtol=MLP_RELOAD_RTOL,
            atol=MLP_RELOAD_ATOL,
        )
        models = []
        for kind, scores in [("lr", ls), ("mlp16", ms)]:
            errors = [
                {
                    "id": r["id"],
                    "expected": r["label"],
                    "predicted": "spam" if score >= DEFAULT_THRESHOLD else "ham",
                    "spam_score": float(score),
                }
                for r, score in zip(validation, scores)
                if (score >= DEFAULT_THRESHOLD) != (r["label"] == "spam")
            ]
            models.append(
                {
                    "kind": kind,
                    "name": "TF-IDF + Logistic Regression" if kind == "lr" else "TF-IDF + MLP 16",
                    "metrics": metric([r["label"] for r in validation], scores),
                    "errors": errors[:ERROR_EXAMPLE_LIMIT],
                    "history": [] if kind == "lr" else history,
                    "checkpoint_epoch": None if kind == "lr" else best["epoch"],
                }
            )
        final = {
            **initial,
            "state": "completed",
            "finished_at": now(),
            "duration_seconds": round(time.monotonic() - started, DURATION_DECIMALS),
            "training_count": len(rows),
            "correction_count": len(snapshot["content"]["corrections"]),
            "imported_count": sum(not r["source"].startswith("uci:") for r in rows),
            "validation_count": len(validation),
            "validation_sha256": sha(canonical(validation)),
            "models": models,
            "safe_numeric_reload_matches": True,
            "artifact": metadata,
            "versions": {
                n: importlib.metadata.version(n) for n in ("torch", "scikit-learn", "numpy")
            },
            "data_audit": dataset["manifest"],
            "scope": "fixed v2 validation; not final test or general accuracy",
        }
        atomic(directory / "report.json", final)
        return final
    except BaseException as exc:
        atomic(
            directory / "report.json",
            {
                **initial,
                "state": "failed",
                "finished_at": now(),
                "error_type": type(exc).__name__,
                "error": "Training did not complete; inspect local worker logs",
                "duration_seconds": round(time.monotonic() - started, DURATION_DECIMALS),
            },
        )
        raise
    finally:
        os.close(worker_fd)


def list_runs(root: str | Path) -> list[dict[str, Any]]:
    """List saved reports and detect interrupted workers without inventing completed runs."""
    root = Path(root)
    runs = []
    for path in sorted((root / "runs").glob("*/report.json")):
        d = read_json(path)
        if d.get("state") == "running":
            fd = os.open(path.parent / ".training.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    d = {
                        **d,
                        "state": "interrupted",
                        "status_note": (
                            "Offline worker exited without a completion record; "
                            "create a new run explicitly"
                        ),
                    }
                except BlockingIOError:
                    pass
            finally:
                os.close(fd)
        runs.append(d)
    return runs


def run_directory(root: str | Path, identity: str) -> Path:
    """Resolve a validated run identity to keep artifact access inside the runs directory."""
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        raise ValueError("Invalid run identity")
    root = Path(root).resolve()
    p = root / "runs" / identity
    if p.is_symlink() or p.resolve().parent != (root / "runs").resolve():
        raise ValueError("Invalid run path")
    return p


def validated_candidate(
    root: str | Path, run_id: str, kind: str, threshold: float
) -> dict[str, Any]:
    """Validate report and artifact agreement before a candidate may be selected."""
    directory = run_directory(root, run_id)
    report = read_json(directory / "report.json")
    if (
        report["state"] != "completed"
        or kind not in {"lr", "mlp16"}
        or not np.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("Candidate is incomplete or invalid")
    model = SafeModel(directory, kind)
    if report["artifact"] != model.metadata or report["run_id"] != run_id:
        raise ValueError("Report and model disagree")
    return {
        "run_id": run_id,
        "kind": kind,
        "threshold": float(threshold),
        "snapshot_id": model.metadata["snapshot_id"],
        "dataset_id": model.metadata["dataset_id"],
        "weights_sha256": model.metadata["weights_sha256"],
        "vocabulary_sha256": model.metadata["vocabulary_sha256"],
    }


def registry(root: str | Path) -> dict[str, Any]:
    """Read selected and previous candidates so activation remains explicit and reversible."""
    path = Path(root) / "service.json"
    return read_json(path) if path.exists() else {"revision": 0, "selected": None, "history": []}


def select(
    root: str | Path,
    run_id: str,
    kind: str = "lr",
    threshold: float = DEFAULT_THRESHOLD,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Select a validated candidate under a lock so concurrent choices cannot overwrite."""
    import fcntl

    candidate = validated_candidate(root, run_id, kind, threshold)
    root = Path(root)
    fd = os.open(root / ".service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        old = registry(root)
        if expected_revision is not None and old["revision"] != expected_revision:
            raise ValueError("Service selection revision changed")
        if old["selected"] == candidate:
            return old
        value = {
            "revision": old["revision"] + 1,
            "selected": candidate,
            "selected_at": now(),
            "history": old["history"]
            + (
                [{"revision": old["revision"], "selected": old["selected"], "replaced_at": now()}]
                if old["selected"]
                else []
            ),
            "activation": "restart required; selected is not necessarily currently loaded",
        }
        atomic(root / "service.json", value)
        return value
    finally:
        os.close(fd)


def rollback(root: str | Path, expected_revision: int | None = None) -> dict[str, Any]:
    """Select the previous candidate while preserving the registry revision check."""
    r = registry(root)
    if not r["history"]:
        raise ValueError("No previous selection")
    p = r["history"][-1]["selected"]
    return select(
        root,
        p["run_id"],
        p["kind"],
        p["threshold"],
        r["revision"] if expected_revision is None else expected_revision,
    )


def load_selected(root: str | Path) -> tuple[dict[str, Any] | None, SafeModel | None]:
    """Verify and load the selected numeric model so serving uses the recorded artifacts."""
    r = registry(root)
    if not r["selected"]:
        return None, None
    selected = r["selected"]
    checked = validated_candidate(root, selected["run_id"], selected["kind"], selected["threshold"])
    if checked != selected:
        raise ValueError("Selected artifact changed")
    return selected, SafeModel(run_directory(root, selected["run_id"]), selected["kind"])


def export_public(root: str | Path, destination: str | Path) -> dict[str, Any]:
    """Export only selected public artifacts to keep private training data off the server."""
    root = Path(root)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Export destination must not exist")
    selected, model = load_selected(root)
    if selected is None:
        raise ValueError("Select a model first")
    destination.mkdir(parents=True)
    run = selected["run_id"]
    target = destination / "runs" / run
    target.mkdir(parents=True)
    for name in ("model.json", "weights.npz", "vocabulary.json", "report.json"):
        shutil.copy2(root / "runs" / run / name, target / name)
    if (root / "final-test-report.json").is_file():
        final = read_json(root / "final-test-report.json")
        if final["candidate"] == selected:
            shutil.copy2(root / "final-test-report.json", destination / "final-test-report.json")
    # Export only selected public reports/artifacts.
    # Keep databases, snapshots, CSV and held-out rows private.
    atomic(
        destination / "service.json",
        {
            "revision": 1,
            "selected": selected,
            "history": [],
            "activation": "selected exported bundle",
        },
    )
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    destination.chmod(0o755)
    return {
        "directory": str(destination),
        "run_id": run,
        "files": sorted(
            str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()
        ),
    }
