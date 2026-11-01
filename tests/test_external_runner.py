"""Exercise external preregistration and one-shot execution on authored temporary fixtures."""

from contextlib import ExitStack, redirect_stderr
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from textlab import external_test as external
from textlab.comparison_budget import Ledger


def authored_rows() -> list[dict]:
    """Create two normalized labeled texts so no test reads the actual external cohort."""
    return [
        {"id": external.sha(text.encode()), "text": text, "label": label, "source_rows": [index]}
        for index, (text, label) in enumerate(
            [("authored meeting fixture", "ham"), ("authored prize fixture", "spam")], 1
        )
    ]


def provider_result(label: str) -> dict:
    """Return typed usage for a mocked provider without creating a transport."""
    return {
        "status": "ok",
        "label": label,
        "failure": False,
        "elapsed_ms": 4.0,
        "model": "deepseek-flash",
        "system_fingerprint": "authored-fingerprint",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 4,
            "total_tokens": 104,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 100,
        },
    }


class ExternalRunnerTests(unittest.TestCase):
    """Keep every path, credential, provider and Git interaction inside controlled fixtures."""

    def setUp(self) -> None:
        """Allocate only temporary source, frozen configuration and report identities."""
        self.temporary = TemporaryDirectory(prefix="textlab-external-runner-")
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.formal = self.project / "var"
        self.public = self.project / "artifacts/external-test"
        self.workspace = self.formal / "external-test-authored"
        self.document = self.project / "docs/EXTERNAL_TEST_PLAN.md"
        self.frozen_path = self.project / "artifacts/llm-comparison/frozen-config.json"
        self.validation_summary = self.project / "artifacts/llm-comparison/summary.json"
        for directory in [
            self.public,
            self.workspace,
            self.document.parent,
            self.frozen_path.parent,
            self.project / "textlab",
        ]:
            directory.mkdir(parents=True, exist_ok=True)
        self.rows = authored_rows()
        self.selected = {
            "kind": "mlp16",
            "threshold": 0.5,
            "run_id": "authored-run",
            "dataset_id": "authored-data",
            "snapshot_id": "authored-snapshot",
            "weights_sha256": "a" * 64,
            "vocabulary_sha256": "b" * 64,
        }
        parameters = external.llm_provider.build_payload("Authored configuration check.")
        parameters.pop("messages")
        self.frozen = {
            "local": self.selected,
            "deepseek": {
                "system_prompt": external.llm_provider.SYSTEM_PROMPT,
                "prompt_sha256": external.llm_provider.PROMPT_SHA256,
                "payload_parameters": parameters,
            },
            "cascade": {"recommended_band": [0.2, 0.8]},
            "text_preprocessing": "authored preprocessing fixture",
        }
        self.frozen_path.write_text(json.dumps(self.frozen))
        self.validation_summary.write_text(json.dumps({"authored_validation": True}))
        (self.formal / "service.json").write_text(json.dumps({"selected": self.selected}))
        for name in [
            "external_test.py",
            "external_data.py",
            "comparison.py",
            "comparison_budget.py",
            "comparison_analysis.py",
            "llm_provider.py",
            "safe_model.py",
            "data.py",
            "dataflow.py",
            "decision.py",
        ]:
            (self.project / "textlab" / name).write_text("# authored source fixture " + name)
        self.audit = {
            "ingestion": {
                "original_rows": 2,
                "ascii_letter_guard_rows_excluded": 0,
                "internal_exact_duplicate_rows_collapsed": 0,
            },
            "dedup": {"uci_overlap_rows_excluded": 0},
            "reserved_identity_exclusion": {"reserved_overlap_rows_excluded": 0},
            "exclusions": [],
        }
        self.stack = ExitStack()
        for name, value in {
            "PROJECT": self.project,
            "FORMAL": self.formal,
            "PUBLIC": self.public,
            "PLAN_DOCUMENT": self.document,
            "FROZEN": self.frozen_path,
            "VALIDATION_SUMMARY": self.validation_summary,
            "REPORT": self.project / "docs/EXTERNAL_TEST.md",
        }.items():
            self.stack.enter_context(patch.object(external, name, value))
        self.stack.enter_context(
            patch.object(external, "configure_guard", return_value=({}, set()))
        )
        self.stack.enter_context(patch.object(external, "fingerprints", return_value={}))

    def tearDown(self) -> None:
        """Restore module paths before removing the isolated fixture tree."""
        self.stack.close()
        self.temporary.cleanup()

    def prepare_fixture(self) -> dict:
        """Freeze a two-row protocol while proving preparation cannot score or read credentials."""
        with (
            patch.object(external, "prepare_cohort", return_value=(self.rows, self.audit)),
            patch.object(external, "online_model_check", return_value={"status": "passed"}),
            patch.object(external, "SafeModel", side_effect=AssertionError("model loading")),
            patch.object(external, "local_results", side_effect=AssertionError("prediction")),
            patch.object(external, "read_key", side_effect=AssertionError("credential read")),
            patch.object(external, "dispatch", side_effect=AssertionError("paid request")),
        ):
            return external.prepare(self.workspace, True)

    def committed_file(self, args: list[str], **_kwargs: object) -> bytes:
        """Return fixture Git contents instead of inspecting the real repository."""
        if args[:2] != ["git", "show"]:
            raise AssertionError("Unexpected Git operation")
        return (self.project / args[2].removeprefix("HEAD:")).read_bytes()

    def test_prepare_is_prediction_free_immutable_and_publicly_text_free(self) -> None:
        """Freeze before inference and keep message-bearing rows exclusively in the private root."""
        plan = self.prepare_fixture()
        self.assertEqual(plan["external_count"], 2)
        self.assertEqual(plan["cascade_band"], [0.2, 0.8])
        self.assertEqual(plan["execution"]["cap_yuan"], "4")
        self.assertEqual(plan["execution"]["authorized_max_cap_yuan"], "4")
        before = self.document.read_bytes()
        for path in self.public.glob("*.json"):
            content = path.read_text()
            for row in self.rows:
                self.assertNotIn(row["text"], content)
        self.assertEqual(
            json.loads((self.workspace / "cohort.private.json").read_text()), self.rows
        )
        external.immutable_bytes(self.document, before)
        with self.assertRaises(ValueError):
            external.immutable_bytes(self.document, before + b"changed")
        self.assertEqual(self.document.read_bytes(), before)

    def test_load_plan_requires_committed_public_and_workspace_identity(self) -> None:
        """Reject private replacements that differ from the committed public preregistration."""
        self.prepare_fixture()
        with patch.object(external.subprocess, "check_output", side_effect=self.committed_file):
            external.load_plan(self.workspace, check_committed=True)
            path = self.workspace / "plan.json"
            value = json.loads(path.read_text())
            value["external_count"] += 1
            config = {k: v for k, v in value.items() if k not in {"plan_sha256", "document_sha256"}}
            value["plan_sha256"] = external.sha(external.canonical(config))
            path.write_text(json.dumps(value, indent=2) + "\n")
            with self.assertRaisesRegex(ValueError, "committed preregistration"):
                external.load_plan(self.workspace, check_committed=True)

    def test_uncommitted_plan_is_rejected_before_execution(self) -> None:
        """Require Git evidence rather than treating a working-tree file as preregistration."""
        self.prepare_fixture()
        with (
            patch.object(
                external.subprocess,
                "check_output",
                side_effect=subprocess.CalledProcessError(128, ["git", "show"]),
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            external.load_plan(self.workspace, check_committed=True)

    def test_source_and_reference_report_drift_are_rejected(self) -> None:
        """Keep both the execution code and validation comparison baseline bound to the plan."""
        self.prepare_fixture()
        original = self.validation_summary.read_bytes()
        self.validation_summary.write_text('{"authored_validation":"changed"}')
        with self.assertRaisesRegex(ValueError, "reference validation report changed"):
            external.load_plan(self.workspace, check_committed=False)
        self.validation_summary.write_bytes(original)
        path = self.project / "textlab/external_data.py"
        path.write_text("# changed authored source")
        with self.assertRaisesRegex(ValueError, "execution source changed"):
            external.load_plan(self.workspace, check_committed=False)

    def test_runtime_prompt_drift_is_rejected_without_a_paid_call(self) -> None:
        """Recheck the actual request prompt even if the frozen JSON and source fixtures match."""
        self.prepare_fixture()
        with (
            patch.object(external.llm_provider, "SYSTEM_PROMPT", "changed authored prompt"),
            self.assertRaisesRegex(ValueError, "system prompt differs"),
        ):
            external.load_plan(self.workspace, check_committed=False)

    def test_one_shot_marker_rejects_identical_and_new_workspace_attempts(self) -> None:
        """Treat any existing marker as consumed so another directory cannot repeat the study."""
        attempt = {"study_id": "authored-study", "plan_sha256": "c" * 64}
        external.reserve_attempt(self.workspace, attempt)
        before = (self.public / "evaluation-attempt.json").read_bytes()
        with self.assertRaises(ValueError):
            external.reserve_attempt(self.workspace, attempt)
        other = self.formal / "external-test-other"
        other.mkdir()
        with self.assertRaises(ValueError):
            external.reserve_attempt(other, attempt)
        self.assertEqual((self.public / "evaluation-attempt.json").read_bytes(), before)

    def test_execute_interruption_preserves_partial_ledger_and_blocks_second_attempt(self) -> None:
        """Save unknown charges once and refuse a second live execution after interruption."""
        plan = self.prepare_fixture()
        observed = {}

        async def interrupted(rows: list[dict], ledger: Ledger, _key: str) -> None:
            observed["cap"] = ledger.cap
            observed["authority"] = ledger.authorized_max_cap_yuan
            ledger.reserve(rows[0]["id"], external.llm_provider.build_payload(rows[0]["text"]))
            raise RuntimeError("authored interruption")

        def publish(_workspace: Path, _plan: dict, ledger: Ledger, _integrity: dict) -> dict:
            observed["records"] = dict(ledger.records)
            return {"requested_rows": 2}

        local = [
            {
                "sample_id": row["id"],
                "truth": row["label"],
                "local": {
                    "status": "ok",
                    "label": row["label"],
                    "spam_score": 0.8 if row["label"] == "spam" else 0.2,
                    "elapsed_ms": 1.0,
                },
            }
            for row in self.rows
        ]
        with (
            patch.object(external, "load_plan", return_value=plan),
            patch.object(external, "read_key", return_value="authored-not-real-key"),
            patch.object(external.subprocess, "check_output", return_value="authored-commit"),
            patch.object(external, "SafeModel", return_value=object()) as model,
            patch.object(external, "local_results", return_value=local),
            patch.object(external, "dispatch", interrupted),
            patch.object(external, "publish", side_effect=publish),
        ):
            with self.assertRaisesRegex(RuntimeError, "partial results saved"):
                external.execute(self.workspace, None)
            self.assertEqual(observed["cap"], Decimal("4"))
            self.assertEqual(observed["authority"], Decimal("4"))
            self.assertEqual(observed["records"][self.rows[0]["id"]]["status"], "unknown")
            with self.assertRaisesRegex(ValueError, "already has an attempt"):
                external.execute(self.workspace, None)
            self.assertEqual(model.call_count, 1)

    def test_publish_keeps_private_ledger_and_only_one_fixed_cascade(self) -> None:
        """Publish text-free results without selecting a new band or replacing accounting state."""
        plan = self.prepare_fixture()
        local = [
            {
                "sample_id": row["id"],
                "truth": row["label"],
                "local": {
                    "status": "ok",
                    "label": row["label"],
                    "spam_score": 0.8 if row["label"] == "spam" else 0.2,
                    "elapsed_ms": 1.0,
                },
            }
            for row in self.rows
        ]
        (self.workspace / "local-results.json").write_text(json.dumps(local))
        ledger_path = self.workspace / "ledger.json"
        ledger = Ledger(
            ledger_path, Decimal("4"), plan["plan_sha256"], authorized_max_cap_yuan=Decimal("4")
        )
        for row in self.rows:
            ledger.reserve(row["id"], external.llm_provider.build_payload(row["text"]))
            ledger.finish(row["id"], provider_result(row["label"]))
        before = ledger_path.read_bytes()
        module = ModuleType("textlab.external_report")
        module.render_report = lambda *_args, **_kwargs: None
        with patch.dict(sys.modules, {"textlab.external_report": module}):
            summary = external.publish(self.workspace, plan, ledger)
            self.assertEqual(ledger_path.read_bytes(), before)
            self.assertEqual(len(summary["cascades"]), 1)
            self.assertEqual(summary["cascades"][0]["band"], [0.2, 0.8])
            self.assertFalse(summary["selection_performed"])
            self.assertNotIn("recommendation", summary)
            self.assertNotIn("best_observed_nonzero_band", summary)
            replayed = external.publish(self.workspace, plan, None)
            self.assertEqual(replayed, summary)
        for row in self.rows:
            self.assertNotIn(row["text"], (self.public / "results.jsonl").read_text())

    def test_main_refuses_model_and_source_roots_before_creating_files(self) -> None:
        """Keep command-line workspaces separate from formal models, source and data directories."""
        invalid = [
            self.formal / "runs/new-root",
            self.project / "data/new-root",
            self.project / "textlab/new-root",
        ]
        for path in invalid:
            with (
                self.subTest(path=path),
                patch.object(sys, "argv", ["external", "--root", str(path), "--prepare"]),
                patch.object(external, "prepare", side_effect=AssertionError("preparation")),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                external.main()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
