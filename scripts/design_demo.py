"""Isolated design demo for the Vue workbench. SIMULATED DATA ONLY.

Serves the production build (frontend/dist) through the real app_v2 with:
- a temporary/explicit state directory built from hand-authored fixtures
  (never var/, .env or project data),
- a fake operator token, and
- an in-process httpx.MockTransport in place of TypeSafe (no network, no paid call).

The fake BYOK value selects the simulated upstream outcome by suffix:
  ...-ok (default), -flip (opposite label), -401, -429, -500, -bad, -nousage,
  -slow (5 s), -hang (exceeds deadline).

Demo-only fault injection (one-shot counts) is read from <root>/demo-faults.json, e.g.
  {"drop_patch": 1, "fail_list": 1, "drop_confirm": 1, "delay": {"POST /api/imports/preview": 4}}
drop_* lets the real write commit, then replaces its response with a non-JSON 502
(write outcome unknown).

Usage: .venv/bin/python scripts/design_demo.py --mode private|public|empty
       --port 8781 --root <scratch dir>
"""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for name in (
    "TYPESAFE_API_KEY",
    "SMS_LAB_ROOT",
    "SMS_LAB_ADMIN_TOKEN",
    "SMS_LAB_PUBLIC",
    "SMS_LAB_JEV_ENABLED",
    "SMS_LAB_JEV_SERVER_KEY_ENABLED",
):
    os.environ.pop(name, None)  # Never pick up a real environment value.

from fastapi import FastAPI  # noqa: E402
from starlette.types import ASGIApp, Message, Receive, Scope, Send  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
import uvicorn  # noqa: E402
from textlab.app_v2 import create_app  # noqa: E402
from textlab.dataflow import DataStore, atomic, immutable, sha, canonical, now  # noqa: E402
from textlab.decision import DecisionService, DecisionSettings, MODEL  # noqa: E402
from textlab import experiments  # noqa: E402
from textlab.safe_model import SafeModel  # noqa: E402
from tests.test_workbench_v2 import seeded  # noqa: E402

HIDDEN_UNITS = 16
HAM_COEFFICIENT = 1.6
SPAM_COEFFICIENT = 1.8
INPUT_WEIGHT_SCALE = 3
OUTPUT_WEIGHT_SCALE = 1.5
DEFAULT_THRESHOLD = 0.5
DEMO_SEED = 42
LOSS_EPOCHS = 12
CHECKPOINT_EPOCH = 9
REVIEW_TEMPLATE_COUNT = 8
TRAIN_LOSS_SCALE = 0.62
TRAIN_LOSS_DECAY = 4
TRAIN_LOSS_FLOOR = 0.05
VALIDATION_LOSS_SCALE = 0.6
VALIDATION_LOSS_DECAY = 5
VALIDATION_LOSS_FLOOR = 0.14
DISPLAY_DIGITS = 4
SLOW_RESPONSE_SECONDS = 5
HANG_RESPONSE_SECONDS = 60
DECISION_DEADLINE_SECONDS = 6


OPERATOR = "demo-operator-token-not-secret"
SERVER_KEY = "demo-server-key-not-secret"
RUN = "demo-synthetic-fixture"
HAM_WORDS = [
    "normal",
    "meeting",
    "reminder",
    "project",
    "tomorrow",
    "afternoon",
    "notebook",
    "appointment",
    "confirmed",
]
SPAM_WORDS = ["prize", "claim", "cash", "offer", "won", "payment", "exclusive"]
SPAMMY = (
    "prize",
    "claim",
    "cash",
    "won",
    "offer",
    "winner",
    "free",
    "exclusive",
    "payment",
    "riches",
)

# Authored English SMS for the demo pool; one row is deliberately mislabeled.
AUTHORED = [
    ("Reminder {i}: please bring the blue notebook to the project meeting tomorrow.", "ham"),
    ("Your appointment {i} is confirmed for Tuesday afternoon at the clinic.", "ham"),
    ("WIN a cash prize #{i}! Reply CLAIM now to receive your exclusive offer.", "spam"),
]


def demo_fixture(root: Path) -> DataStore:
    """Build authored weights, curves and two-row metrics without running training."""
    store = seeded(root)
    snapshot = store.snapshot()
    directory = root / "runs" / RUN
    directory.mkdir(parents=True)
    words = HAM_WORDS + SPAM_WORDS
    coef = np.array([-HAM_COEFFICIENT] * len(HAM_WORDS) + [SPAM_COEFFICIENT] * len(SPAM_WORDS))
    immutable(
        directory / "vocabulary.json",
        {"vocabulary": {w: i for i, w in enumerate(words)}, "idf": [1.0] * len(words)},
    )
    np.savez_compressed(
        directory / "weights.npz",
        lr_coef=coef,
        lr_intercept=np.zeros(1),
        mlp_w1=np.eye(HIDDEN_UNITS, len(words)) * INPUT_WEIGHT_SCALE,
        mlp_b1=np.zeros(HIDDEN_UNITS),
        mlp_w2=coef.reshape(1, -1)[:, :HIDDEN_UNITS] * OUTPUT_WEIGHT_SCALE,
        mlp_b2=np.zeros(1),
    )
    metadata = {
        "run_id": RUN,
        "snapshot_id": snapshot["snapshot_id"],
        "dataset_id": "fixture-v2",
        "weights_sha256": sha((directory / "weights.npz").read_bytes()),
        "vocabulary_sha256": sha((directory / "vocabulary.json").read_bytes()),
    }
    immutable(directory / "model.json", metadata)
    validation = store.require_prepared()["validation"]
    models = []
    for kind in ("lr", "mlp16"):
        scores = SafeModel(directory, kind).scores([r["text"] for r in validation])
        history = (
            []
            if kind == "lr"
            else [
                {
                    "epoch": e,
                    "training_loss": round(
                        TRAIN_LOSS_SCALE * math.exp(-e / TRAIN_LOSS_DECAY) + TRAIN_LOSS_FLOOR,
                        DISPLAY_DIGITS,
                    ),
                    "validation_loss": round(
                        VALIDATION_LOSS_SCALE * math.exp(-e / VALIDATION_LOSS_DECAY)
                        + VALIDATION_LOSS_FLOOR,
                        DISPLAY_DIGITS,
                    ),
                }
                for e in range(1, LOSS_EPOCHS + 1)
            ]
        )
        models.append(
            {
                "kind": kind,
                "name": "Authored LR weights" if kind == "lr" else "Authored MLP 16 weights",
                "metrics": experiments.metric([r["label"] for r in validation], scores),
                "errors": [],
                "history": history,
                "checkpoint_epoch": None if kind == "lr" else CHECKPOINT_EPOCH,
            }
        )
    immutable(
        directory / "report.json",
        {
            "run_id": RUN,
            "state": "completed",
            "snapshot_id": snapshot["snapshot_id"],
            "dataset_id": "fixture-v2",
            "artifact": metadata,
            "config": {"threshold": DEFAULT_THRESHOLD, "seed": DEMO_SEED},
            "code_sha256": "synthetic-demo-no-training",
            "validation_sha256": sha(canonical(validation)),
            "evaluation_split": "validation",
            "test_set_evaluated": False,
            "training_count": 2,
            "imported_count": 0,
            "correction_count": 0,
            "validation_count": len(validation),
            "models": models,
            "safe_numeric_reload_matches": True,
            "data_audit": {
                "rule": "SYNTHETIC DESIGN DEMO fixture (2 train / 2 validation authored rows)"
            },
            "scope": (
                "SYNTHETIC DESIGN DEMO: hand-authored weights and loss curve; "
                "no training ran; two-row synthetic validation."
            ),
        },
    )
    candidate = experiments.select(root, RUN, "lr", DEFAULT_THRESHOLD)["selected"]
    final_rows = [
        ("synthetic final guard normal meeting example", "ham"),
        ("synthetic final guard cash prize example", "spam"),
    ]
    final_scores = SafeModel(directory, "lr").scores([t for t, _ in final_rows])
    atomic(
        root / "final-test-report.json",
        {
            "schema_version": 1,
            "evaluation_split": "test",
            "completed_at": now(),
            "candidate": candidate,
            "metrics": experiments.metric([label for _, label in final_rows], final_scores),
            "test_texts_or_error_examples_disclosed": False,
            "scope": (
                "SYNTHETIC DESIGN DEMO: two authored rows; NOT the historical 757-row final test"
            ),
            "no_further_model_selection": True,
        },
    )
    lines = ["text,label"] + [
        f'"{template.format(i=i)}",{label}'
        for i in range(1, REVIEW_TEMPLATE_COUNT + 1)
        for template, label in AUTHORED
    ]
    lines.append(
        '"Congratulations, you won a free cruise! Call now to claim your cash prize.",ham'
    )  # deliberate mislabel
    preview = store.preview_csv("\n".join(lines) + "\n", "SYNTHETIC DESIGN DEMO authored rows")
    store.confirm_import(preview["preview_id"])
    return store


def mock_transport() -> httpx.MockTransport:
    """Simulate TypeSafe outcomes in process so the demo never makes paid requests."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return authored upstream outcomes to exercise the decision interface safely."""
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        text = json.loads(request.content)["state"].casefold()
        label = "spam" if any(word in text for word in SPAMMY) else "ham"
        if key.endswith("-flip"):
            label = "ham" if label == "spam" else "spam"
        if key.endswith("-401"):
            return httpx.Response(401, json={"error": "simulated"})
        if key.endswith("-429"):
            return httpx.Response(429, json={"error": "simulated"})
        if key.endswith("-500"):
            return httpx.Response(503, json={"error": "simulated"})
        if key.endswith("-bad"):
            return httpx.Response(200, json={"model": "jev-latest"})
        if key.endswith("-slow"):
            await asyncio.sleep(SLOW_RESPONSE_SECONDS)
        if key.endswith("-hang"):
            await asyncio.sleep(HANG_RESPONSE_SECONDS)
        probabilities = (
            {"ham": 0.12, "spam": 0.88} if label == "spam" else {"ham": 0.91, "spam": 0.09}
        )
        payload = {
            "model": MODEL,
            "answers": {
                "sms_class": {
                    "type": "choice",
                    "choice": label,
                    "probabilities": probabilities,
                    "confidence": round(2 * max(probabilities.values()) - 1, DISPLAY_DIGITS),
                }
            },
        }
        if not key.endswith("-nousage"):
            payload["usage"] = {"input_tokens": 60 + len(text) // 4, "output_tokens": 9}
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


class FaultInjector:
    """Lose a committed write response to exercise the demo's uncertain-save UI."""

    RULES = {
        ("PATCH", "/api/samples/"): "drop_patch",
        ("POST", "/api/imports/confirm"): "drop_confirm",
        ("GET", "/api/samples"): "fail_list",
    }

    def __init__(self, app: ASGIApp, flags: Path) -> None:
        """Bind one-shot demo faults without changing the wrapped application."""
        self.app, self.flags = app, flags

    def take(self, method: str, path: str) -> tuple[str | None, float]:
        """Consume one matching fault count so each simulated failure happens once."""
        if not self.flags.exists():
            return None, 0
        flags = json.loads(self.flags.read_text() or "{}")
        delay = flags.get("delay", {}).get(f"{method} {path}", 0)
        for (m, prefix), name in self.RULES.items():
            exact = name == "fail_list"
            if (
                method == m
                and (path == prefix if exact else path.startswith(prefix))
                and flags.get(name, 0) > 0
            ):
                flags[name] -= 1
                self.flags.write_text(json.dumps(flags))
                return name, delay
        return None, delay

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Inject demo delays and lost responses to exercise uncertain-save handling."""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        fault, delay = self.take(scope["method"], scope["path"])
        if delay:
            await asyncio.sleep(delay)
        if fault and fault.startswith("drop_"):

            async def swallow(_message: Message) -> None:
                """Discard the committed response to simulate an unknown write outcome."""
                return None

            await self.app(scope, receive, swallow)
        if fault:
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [(b"content-type", b"text/html")],
                }
            )
            return await send(
                {"type": "http.response.body", "body": b"<html>simulated gateway fault</html>"}
            )
        return await self.app(scope, receive, send)


def build(mode: str, root: Path | str) -> tuple[FastAPI, Path]:
    """Build an isolated synthetic demo and reject any project data directory."""
    root = Path(root).resolve()
    if root == ROOT or ROOT in root.parents:
        raise SystemExit("Use a scratch directory outside the project; never var/ or project data")
    transport = mock_transport()
    if mode == "empty":
        if not (root / "dataset.json").exists():
            root.mkdir(parents=True, exist_ok=True)
            seeded(root)
        return create_app(
            root, OPERATOR, False, DecisionService(DecisionSettings(False, False), transport)
        ), root
    private = root / "private"
    if not (private / "service.json").exists():
        private.mkdir(parents=True, exist_ok=True)
        demo_fixture(private)
    if mode == "public":
        bundle = root / "public-bundle"
        if not bundle.exists():
            experiments.export_public(private, bundle)
        return create_app(
            bundle,
            OPERATOR,
            True,
            DecisionService(
                DecisionSettings(True, False), transport, deadline_seconds=DECISION_DEADLINE_SECONDS
            ),
        ), private
    os.environ["TYPESAFE_API_KEY"] = (
        SERVER_KEY  # fake value; the mock transport never leaves the process
    )
    return create_app(
        private,
        OPERATOR,
        False,
        DecisionService(
            DecisionSettings(True, True), transport, deadline_seconds=DECISION_DEADLINE_SECONDS
        ),
    ), private


def main() -> None:
    """Serve the synthetic demo only when the CLI is explicitly invoked."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("private", "public", "empty"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--root",
        required=True,
        help="scratch directory for simulated state (never the project var/)",
    )
    args = parser.parse_args()
    app, state = build(args.mode, args.root)
    print(
        f"SIMULATED DATA ONLY · mode={args.mode} · state={state} · operator token={OPERATOR}",
        flush=True,
    )
    uvicorn.run(
        FaultInjector(app, Path(state) / "demo-faults.json"),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
