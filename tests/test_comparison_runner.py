"""Check comparison orchestration with authored temporary fixtures and mocked transports."""

import asyncio
from contextlib import redirect_stdout
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np

from textlab import comparison
from textlab.comparison_budget import Ledger, estimate_request

IDENTITY = hashlib.sha256(b"authored-runner-fixture").hexdigest()
USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 4,
    "total_tokens": 104,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 100,
}


def rows(count: int = 6) -> list[dict]:
    """Create unique authored SMS so orchestration tests never need the real validation set."""
    result = []
    for index in range(count):
        text = f"authored fixture message {index} " + ("prize" if index % 2 else "meeting")
        result.append(
            {
                "id": hashlib.sha256(text.encode()).hexdigest(),
                "text": text,
                "label": "spam" if index % 2 else "ham",
            }
        )
    return result


def result_for(text: str) -> dict:
    """Return a small typed response for a mocked call with consistent token usage."""
    return {
        "status": "ok",
        "label": "spam" if "prize" in text else "ham",
        "failure": False,
        "elapsed_ms": 10.0,
        "http_status": 200,
        "usage": dict(USAGE),
        "model": "deepseek-flash",
        "system_fingerprint": "authored-fingerprint",
    }


class FakeClient:
    """Stand in for an HTTP context so an accidental unmocked dispatch cannot use a client."""

    def __init__(self, **_kwargs: object) -> None:
        """Accept client configuration without opening a transport."""

    async def __aenter__(self) -> "FakeClient":
        """Return only this transport-free fixture for mocked provider functions."""
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Close the no-op fixture without making network requests."""


class FakeNumericModel:
    """Produce obvious authored scores so local evidence can be checked without weights."""

    def scores(self, texts: list[str]) -> np.ndarray:
        """Map authored fixture words to finite numeric scores for local orchestration."""
        return np.asarray([0.8 if "prize" in text else 0.2 for text in texts])


def reporter_fixture() -> ModuleType:
    """Avoid rendering a document while testing the runner's cached publication boundary."""
    module = ModuleType("textlab.comparison_report")
    module.render_report = lambda *_args, **_kwargs: None
    return module


class ValidationAndGuardTests(unittest.TestCase):
    """Validate identities and run filesystem audit checks inside a disposable interpreter."""

    def test_validation_identity_count_and_class_checks(self) -> None:
        """Reject malformed subsets before any API dispatch or model inference is possible."""
        with TemporaryDirectory(prefix="textlab-runner-validation-") as temporary:
            path = Path(temporary) / "dataset.json"
            valid = {"manifest": {"dataset_id": "authored-data"}, "validation": rows(2)}
            path.write_text(json.dumps(valid))
            manifest, validation = comparison.read_validation(path, expected_count=2)
            self.assertEqual(manifest, valid["manifest"])
            self.assertEqual(validation, valid["validation"])
            invalid = [
                {**valid, "validation": rows(1)},
                {**valid, "validation": [rows(2)[0], rows(2)[0]]},
                {**valid, "validation": [{**r, "id": "incorrect"} for r in rows(2)]},
                {**valid, "validation": [{**r, "label": "invalid"} for r in rows(2)]},
                {**valid, "validation": [{**r, "label": "ham"} for r in rows(2)]},
            ]
            for value in invalid:
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    comparison.read_validation(path, expected_count=2)

    def test_formal_allowlist_blocks_sealed_reads_and_all_input_writes(self) -> None:
        """Exercise the irreversible audit hook in a child that owns only authored temp files."""
        script = """
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from textlab import comparison
with TemporaryDirectory(prefix="textlab-runner-audit-") as temporary:
    root = Path(temporary).resolve()
    comparison.FORMAL = root / "var"
    comparison.FORMAL.mkdir()
    workspace = comparison.FORMAL / "independent-comparison"
    workspace.mkdir()
    permitted = comparison.FORMAL / "dataset.json"
    permitted.write_text("authored permitted fixture")
    sealed = comparison.FORMAL / "heldout-test.json"
    sealed.write_text("authored sealed fixture")
    (workspace / "escape").symlink_to(sealed)
    audit = comparison.protect_formal_files(workspace, {permitted.resolve()})
    assert permitted.read_text() == "authored permitted fixture"
    denied = [lambda: sealed.read_bytes(), lambda: permitted.write_text("overwrite"),
              lambda: permitted.chmod(0o600), lambda: (workspace / "escape").read_bytes(),
              lambda: (comparison.FORMAL / "unexpected.json").write_text("new")]
    for operation in denied:
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise AssertionError("Forbidden formal operation was allowed")
    (workspace / "own-evidence.json").write_text("authored workspace output")
    assert audit["blocked_formal_accesses"] == len(denied)
    assert audit["heldout_test_files_opened"] == 0
    print("audit fixtures passed")
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("audit fixtures passed", completed.stdout)

    def test_local_results_contain_no_text_or_mutation(self) -> None:
        """Keep only IDs, truth and numeric predictions in cached local evidence."""
        validation = rows(2)
        original = json.loads(json.dumps(validation))
        outputs = comparison.local_results(FakeNumericModel(), validation)
        self.assertEqual(validation, original)
        self.assertEqual([r["sample_id"] for r in outputs], [r["id"] for r in validation])
        self.assertEqual([r["local"]["label"] for r in outputs], ["ham", "spam"])
        for row in validation:
            self.assertNotIn(row["text"], json.dumps(outputs))


class PreparationTests(unittest.TestCase):
    """Freeze synthetic configuration and check dry-run boundaries without formal inputs."""

    def test_cold_prepare_freezes_identity_and_cached_local_results(self) -> None:
        """Freeze input and source identity so a later run cannot silently change the experiment."""
        with TemporaryDirectory(prefix="textlab-runner-prepare-") as temporary:
            project = Path(temporary).resolve()
            formal = project / "var"
            directory = formal / "runs/authored-run"
            directory.mkdir(parents=True)
            texts = rows(2)
            dataset = {"manifest": {"dataset_id": "authored-data"}, "validation": texts}
            (formal / "dataset.json").write_text(json.dumps(dataset))
            selected = {
                "kind": "mlp16",
                "threshold": 0.5,
                "run_id": "authored-run",
                "dataset_id": "authored-data",
                "snapshot_id": "authored-snapshot",
            }
            for name, field in [
                ("weights.npz", "weights_sha256"),
                ("vocabulary.json", "vocabulary_sha256"),
            ]:
                content = ("authored fixture " + name).encode()
                (directory / name).write_bytes(content)
                selected[field] = hashlib.sha256(content).hexdigest()
            metadata = {k: v for k, v in selected.items() if k not in {"kind", "threshold"}}
            (directory / "model.json").write_text(json.dumps(metadata))
            (formal / "service.json").write_text(json.dumps({"selected": selected}))
            package = project / "textlab"
            package.mkdir()
            for name in [
                "comparison.py",
                "comparison_budget.py",
                "comparison_analysis.py",
                "comparison_report.py",
                "llm_provider.py",
                "safe_model.py",
                "data.py",
            ]:
                (package / name).write_text("# authored source fixture " + name + "\n")
            workspace = formal / "independent-comparison"
            model = FakeNumericModel()
            model.metadata = metadata
            original_read = comparison.read_validation
            with (
                patch.object(comparison, "PROJECT", project),
                patch.object(comparison, "FORMAL", formal),
                patch.object(comparison, "VALIDATION_COUNT", 2),
                patch.object(comparison, "protect_formal_files", return_value={}),
                patch.object(comparison, "SafeModel", return_value=model),
                patch.object(
                    comparison,
                    "read_validation",
                    side_effect=lambda path: original_read(path, expected_count=2),
                ),
                patch.object(comparison, "online_model_check", side_effect=AssertionError("HTTP")),
                patch.object(comparison, "read_key", side_effect=AssertionError("credential")),
                patch.object(
                    comparison.llm_provider, "classify", side_effect=AssertionError("paid call")
                ),
            ):
                first, validation, _audit, allowed = comparison.prepare(workspace, False)
                self.assertEqual(validation, texts)
                self.assertNotIn(formal / "heldout-test.json", allowed)
                local_bytes = (workspace / "local-results.json").read_bytes()
                second, *_rest = comparison.prepare(workspace, False)
                self.assertEqual(first["identity_sha256"], second["identity_sha256"])
                self.assertEqual((workspace / "local-results.json").read_bytes(), local_bytes)
                self.assertEqual(first["online_model_check"]["status"], "not_requested")
                evidence = (workspace / "plan.json").read_text() + local_bytes.decode()
                for row in texts:
                    self.assertNotIn(row["text"], evidence)
                (package / "comparison_analysis.py").write_text("# changed authored fixture\n")
                with self.assertRaisesRegex(ValueError, "configuration differs"):
                    comparison.prepare(workspace, False)

    def test_main_dry_run_does_not_read_credentials_or_dispatch(self) -> None:
        """Prepare and print a dry-run estimate without creating a paid ledger or reading a key."""
        with TemporaryDirectory(prefix="textlab-runner-dry-") as temporary:
            workspace = Path(temporary) / "new-independent-workspace"
            plan = {"dry_run": {"heuristic_peak_cost_yuan": "0.5"}}
            with (
                patch.object(comparison, "prepare", return_value=(plan, rows(2), {}, set())),
                patch.object(comparison, "Ledger", side_effect=AssertionError("paid ledger")),
                patch.object(comparison, "read_key", side_effect=AssertionError("key read")),
                patch.object(comparison, "dispatch", side_effect=AssertionError("paid call")),
                patch.object(sys, "argv", ["comparison", "--root", str(workspace)]),
                redirect_stdout(io.StringIO()) as output,
            ):
                comparison.main()
            self.assertIn('"mode": "dry_run"', output.getvalue())
            self.assertFalse((workspace / "ledger.json").exists())


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    """Check concurrent reservations and no-retry behavior without a real HTTP transport."""

    async def test_resume_skips_interrupted_and_completed_ids(self) -> None:
        """Treat every durable dispatch as used so resume cannot replay an unknown paid request."""
        with TemporaryDirectory(prefix="textlab-runner-resume-") as temporary:
            validation = rows()
            ledger = Ledger(Path(temporary) / "ledger.json", Decimal("1"), IDENTITY)
            for row in validation[:2]:
                ledger.reserve(row["id"], comparison.llm_provider.build_payload(row["text"]))
            ledger.finish(validation[1]["id"], result_for(validation[1]["text"]))
            calls = []

            async def classify(_client: object, _key: str, text: str) -> dict:
                calls.append(text)
                await asyncio.sleep(0)
                return result_for(text)

            with (
                patch.object(comparison.httpx, "AsyncClient", FakeClient),
                patch.object(comparison.llm_provider, "classify", classify),
                redirect_stdout(io.StringIO()),
            ):
                await comparison.dispatch(validation, ledger, "authored-dummy-key")
            self.assertEqual(set(calls), {r["text"] for r in validation[2:]})
            self.assertEqual(len(calls), len(set(calls)))
            self.assertEqual(ledger.records[validation[0]["id"]]["status"], "unknown")
            self.assertEqual(ledger.records[validation[1]["id"]]["status"], "ok")
            self.assertEqual(len(ledger.records), len(validation))
            self.assertFalse(any(r["status"] == "dispatching" for r in ledger.records.values()))

    async def test_shared_cap_bounds_unknown_concurrent_calls(self) -> None:
        """Reserve before every await so four workers cannot collectively exceed the cap."""
        with TemporaryDirectory(prefix="textlab-runner-cap-") as temporary:
            validation = rows(8)
            reserves = [
                Decimal(
                    estimate_request(comparison.llm_provider.build_payload(r["text"]))[
                        "reservation_peak_yuan"
                    ]
                )
                for r in validation
            ]
            cap = sum(reserves[:3])
            ledger = Ledger(Path(temporary) / "ledger.json", cap, IDENTITY)
            calls = []
            exposures = []

            async def classify(_client: object, _key: str, text: str) -> dict:
                calls.append(text)
                exposures.append(Decimal(ledger.summary()["cap_exposure_yuan"]))
                await asyncio.sleep(0)
                return {"status": "unknown", "label": None, "failure": True, "usage": None}

            with (
                patch.object(comparison.httpx, "AsyncClient", FakeClient),
                patch.object(comparison.llm_provider, "classify", classify),
                redirect_stdout(io.StringIO()),
            ):
                await comparison.dispatch(validation, ledger, "authored-dummy-key")
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(exposure <= cap for exposure in exposures))
            self.assertLessEqual(Decimal(ledger.summary()["cap_exposure_yuan"]), cap)
            self.assertEqual(ledger.summary()["unknown_usage_count"], 3)
            self.assertEqual(len(calls), len(set(calls)))

    async def test_credential_rejection_stops_after_the_inflight_group(self) -> None:
        """Stop new identities after authorization failure while retaining current reservations."""
        with TemporaryDirectory(prefix="textlab-runner-rejection-") as temporary:
            validation = rows(9)
            ledger = Ledger(Path(temporary) / "ledger.json", Decimal("1"), IDENTITY)
            calls = []

            async def classify(_client: object, _key: str, text: str) -> dict:
                calls.append(text)
                await asyncio.sleep(0)
                return {
                    "status": "unknown",
                    "label": None,
                    "failure": True,
                    "http_status": 401,
                    "usage": None,
                }

            with (
                patch.object(comparison.httpx, "AsyncClient", FakeClient),
                patch.object(comparison.llm_provider, "classify", classify),
                redirect_stdout(io.StringIO()),
            ):
                await comparison.dispatch(validation, ledger, "authored-dummy-key")
            self.assertEqual(len(calls), comparison.CONCURRENCY)
            self.assertEqual(len(ledger.records), comparison.CONCURRENCY)
            self.assertTrue(all(r["status"] == "unknown" for r in ledger.records.values()))


class PublishAndReplayTests(unittest.TestCase):
    """Keep durable accounting usable while regenerating text-free comparison artifacts."""

    def test_publish_preserves_reloadable_ledger_and_replay_makes_no_calls(self) -> None:
        """Keep the private accounting schema intact so publishing cannot break resume."""
        with TemporaryDirectory(prefix="textlab-runner-publish-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            validation = rows(2)
            local = comparison.local_results(FakeNumericModel(), validation)
            (workspace / "local-results.json").write_text(json.dumps(local))
            ledger_path = workspace / "ledger.json"
            ledger = Ledger(ledger_path, Decimal("1"), IDENTITY)
            for row in validation:
                ledger.reserve(row["id"], comparison.llm_provider.build_payload(row["text"]))
                ledger.finish(row["id"], result_for(row["text"]))
            before = ledger_path.read_bytes()
            plan = {"validation_sha256": "authored-validation-digest", "identity_sha256": IDENTITY}
            with (
                patch.object(comparison, "ARTIFACTS", root / "artifacts"),
                patch.object(comparison, "REPORT", root / "report.md"),
                patch.dict(sys.modules, {"textlab.comparison_report": reporter_fixture()}),
            ):
                summary = comparison.publish(workspace, plan, ledger)
                self.assertEqual(ledger_path.read_bytes(), before)
                restored = Ledger(ledger_path, Decimal("1"), IDENTITY)
                self.assertEqual(restored.summary(), ledger.summary())
                with (
                    patch.object(comparison, "prepare", side_effect=AssertionError("formal read")),
                    patch.object(comparison, "read_key", side_effect=AssertionError("key read")),
                    patch.object(comparison, "dispatch", side_effect=AssertionError("paid call")),
                    patch.object(sys, "argv", ["comparison", "--root", str(workspace), "--replay"]),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    comparison.main()
                self.assertIn('"network_requests": 0', output.getvalue())
                replayed = json.loads((workspace / "summary.json").read_text())
                self.assertEqual(replayed, summary)
            for path in [workspace / "results.jsonl", root / "artifacts/results.jsonl"]:
                evidence = path.read_text()
                for row in validation:
                    self.assertNotIn(row["text"], evidence)


if __name__ == "__main__":
    unittest.main()
