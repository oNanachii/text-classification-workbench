"""Temporary public-bundle tests keep benchmark overrides out of production defaults."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from textlab import app_v2
from textlab import load_test_server as server
from textlab.decision import DecisionSettings, DecisionService

RUN_ID = "synthetic-load-model"
SELECTED = {"run_id": RUN_ID, "kind": "mlp16", "threshold": 0.5}
LOADED = {**SELECTED, "snapshot_id": "synthetic-snapshot", "dataset_id": "synthetic-data"}
MARKER = {"schema_version": 1, "purpose": "local-load-test", "public_bundle": True}


def bundle(root: Path) -> None:
    """Create filenames and authored metadata only; no actual model or formal SMS is read."""
    root.mkdir(parents=True, exist_ok=True)
    (root / server.MARKER_NAME).write_text(json.dumps(MARKER))
    (root / "service.json").write_text(json.dumps({"selected": SELECTED}))
    directory = root / "runs" / RUN_ID
    directory.mkdir(parents=True)
    for name in server.PUBLIC_RUN_FILES:
        (directory / name).write_bytes(b"synthetic-placeholder-not-a-real-model")


def environment(root: Path) -> dict[str, str]:
    """Supply only the three explicit launch inputs, without consulting credential variables."""
    return {
        "SMS_LAB_LOAD_TEST": "true",
        "SMS_LAB_LOAD_TEST_HOST": "127.0.0.1",
        "SMS_LAB_ROOT": str(root),
    }


class FakeModel:
    def predict(self, text: str, threshold: float) -> dict:
        """Return a fixed authored result so HTTP checks cannot load numeric model files."""
        return {"label": "ham", "spam_score": 0.25, "threshold": threshold}


class LoadTestServerTests(unittest.TestCase):
    def setUp(self) -> None:
        """Allocate only a temporary authored public bundle for each independent test."""
        self.temporary = TemporaryDirectory(prefix="textlab-load-server-test-")
        self.root = Path(self.temporary.name) / "public-bundle"
        bundle(self.root)

    def tearDown(self) -> None:
        """Remove only the temporary fixture, never a real model or work database."""
        self.temporary.cleanup()

    def test_import_and_subprocess_override_leave_parent_defaults_unchanged(self) -> None:
        """Use process isolation to prove that relaxed minute limits never escape the harness."""
        before = app_v2.PREDICTIONS_PER_MINUTE, app_v2.PREDICTION_CONCURRENCY
        code = (
            "import json; from textlab import app_v2, load_test_server; "
            "before=(app_v2.PREDICTIONS_PER_MINUTE, app_v2.PREDICTION_CONCURRENCY); "
            "app=load_test_server.create_app(); "
            "print(json.dumps({'before':before,'after':"
            "[app_v2.PREDICTIONS_PER_MINUTE,app_v2.PREDICTION_CONCURRENCY],"
            "'config':app.state.load_test_config}))"
        )
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", **environment(self.root)}
        output = subprocess.check_output(
            [sys.executable, "-B", "-c", code], cwd=server.PROJECT, env=env, text=True
        )
        values = json.loads(output)
        self.assertEqual(values["before"], [30, 2])
        self.assertEqual(values["after"], [server.LOAD_TEST_REQUESTS_PER_MINUTE, 2])
        self.assertEqual(values["config"]["prediction_slots"], 2)
        self.assertEqual(values["config"]["host"], "127.0.0.1")
        self.assertEqual((app_v2.PREDICTIONS_PER_MINUTE, app_v2.PREDICTION_CONCURRENCY), before)

    def test_opt_in_explicit_loopback_and_root_are_mandatory(self) -> None:
        """Reject normal application startup and public interfaces before any bundle access."""
        base = environment(self.root)
        cases = [
            {key: value for key, value in base.items() if key != "SMS_LAB_LOAD_TEST"},
            {**base, "SMS_LAB_LOAD_TEST": "false"},
            {**base, "SMS_LAB_LOAD_TEST_HOST": "0.0.0.0"},
            {**base, "SMS_LAB_LOAD_TEST_HOST": "::"},
            {**base, "SMS_LAB_LOAD_TEST_HOST": "localhost"},
            {key: value for key, value in base.items() if key != "SMS_LAB_ROOT"},
        ]
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with patch.object(server, "_read_metadata") as read:
                    with self.assertRaises(ValueError):
                        server.create_app()
                    read.assert_not_called()
        self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)
        self.assertEqual(app_v2.PREDICTION_CONCURRENCY, 2)

    def test_formal_workspace_and_its_ancestors_are_rejected_without_opening_files(self) -> None:
        """Prevent an accidental root fallback from inspecting real data or a project directory."""
        formal = Path(self.temporary.name) / "synthetic-formal"
        formal.mkdir()
        formal = formal.resolve()
        child = formal / "child"
        child.mkdir()
        with patch.object(server, "FORMAL_ROOT", formal):
            for root in (formal, child, formal.parent):
                with patch.dict(os.environ, environment(root), clear=True):
                    with patch.object(server, "_read_metadata") as read:
                        with self.assertRaises(ValueError):
                            server.create_app()
                        read.assert_not_called()

    def test_marker_is_exact_and_requires_a_real_boolean(self) -> None:
        """Treat a fixture marker as an explicit protocol opt-in rather than a loose truthy flag."""
        for marker in (
            {},
            {**MARKER, "public_bundle": 1},
            {**MARKER, "schema_version": True},
            {**MARKER, "purpose": "production"},
            {**MARKER, "extra": "not-allowed"},
        ):
            (self.root / server.MARKER_NAME).write_text(json.dumps(marker))
            with patch.dict(os.environ, environment(self.root), clear=True):
                with self.assertRaises(ValueError):
                    server.create_app()
        self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)

    def test_unexpected_private_or_historical_files_are_never_read(self) -> None:
        """Allow only the public numeric bundle and exclude old test reports or data stores."""
        original = Path.read_bytes
        for name in (
            "dataset.json",
            "heldout-test.json",
            "review.sqlite3",
            "final-plan.json",
            "final-test-report.json",
            ".env",
        ):
            unexpected = self.root / name
            unexpected.write_text("authored-secret-fixture-must-not-be-read")
            with patch.dict(os.environ, environment(self.root), clear=True):
                with patch.object(
                    Path, "read_bytes", autospec=True, side_effect=lambda path: original(path)
                ) as reads:
                    with self.assertRaises(ValueError):
                        server.create_app()
                    opened = {call.args[0].name for call in reads.call_args_list}
                    self.assertEqual(opened, {server.MARKER_NAME, "service.json"})
            unexpected.unlink()

    def test_symlinks_missing_files_and_invalid_run_ids_are_rejected(self) -> None:
        """Reject path escapes and incomplete bundles before constructing the public service."""
        alias = self.root.parent / "bundle-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with patch.dict(os.environ, environment(alias), clear=True):
            with self.assertRaises(ValueError):
                server.create_app()
        weight = self.root / "runs" / RUN_ID / "weights.npz"
        weight.unlink()
        with patch.dict(os.environ, environment(self.root), clear=True):
            with self.assertRaises(ValueError):
                server.create_app()
        outside = self.root.parent / "outside-weight"
        outside.write_bytes(b"synthetic")
        weight.symlink_to(outside)
        with patch.dict(os.environ, environment(self.root), clear=True):
            with self.assertRaises(ValueError):
                server.create_app()
        weight.unlink()
        weight.write_bytes(b"synthetic")
        (self.root / "service.json").write_text(
            json.dumps({"selected": {**SELECTED, "run_id": "../not-allowed"}})
        )
        with patch.dict(os.environ, environment(self.root), clear=True):
            with self.assertRaises(ValueError):
                server.create_app()

    def test_factory_arguments_force_public_read_only_and_disabled_external_service(self) -> None:
        """Avoid credential-environment lookups by supplying explicit public factory arguments."""
        app = FastAPI()
        with patch.dict(os.environ, environment(self.root), clear=True):
            with patch.object(app_v2, "PREDICTIONS_PER_MINUTE", 30):
                with patch.object(app_v2, "create_app", return_value=app) as factory:
                    with patch.object(
                        DecisionSettings,
                        "environment",
                        side_effect=AssertionError("No credential config lookup"),
                    ):
                        result = server.create_app()
                        self.assertEqual(
                            app_v2.PREDICTIONS_PER_MINUTE, server.LOAD_TEST_REQUESTS_PER_MINUTE
                        )
                        self.assertEqual(app_v2.PREDICTION_CONCURRENCY, 2)
                kwargs = factory.call_args.kwargs
                self.assertEqual(kwargs["root"], self.root.resolve())
                self.assertIs(kwargs["public"], True)
                self.assertEqual(kwargs["admin_token"], "")
                self.assertFalse(kwargs["decision_service"].settings.enabled)
                self.assertFalse(kwargs["decision_service"].settings.server_key_enabled)
                self.assertIs(result, app)
        self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)

    def test_slot_drift_and_failed_factory_do_not_relax_anything(self) -> None:
        """Preserve two production slots and restore the old rate after construction failure."""
        with patch.dict(os.environ, environment(self.root), clear=True):
            with patch.object(app_v2, "PREDICTION_CONCURRENCY", 50):
                with self.assertRaises(ValueError):
                    server.create_app()
            self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)
            with patch.object(app_v2, "create_app", side_effect=RuntimeError("authored failure")):
                with self.assertRaises(RuntimeError):
                    server.create_app()
            self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)
        self.assertEqual(app_v2.PREDICTION_CONCURRENCY, 2)

    def test_os_metrics_normalize_rss_units_and_preserve_cpu_seconds(self) -> None:
        """Normalize native RSS and retain process CPU counters without polling."""
        usage = SimpleNamespace(ru_utime=1.25, ru_stime=0.75, ru_maxrss=4096)
        for platform, expected in (("darwin", 4096), ("linux", 4096 * 1024)):
            with self.subTest(platform=platform), patch.object(server.sys, "platform", platform):
                with patch.object(server.resource, "getrusage", return_value=usage) as read:
                    with patch.object(
                        server, "_thread_count", return_value=(7, "synthetic-native")
                    ):
                        with patch.object(server, "_threadpool_info", return_value=(False, [])):
                            values = server._process_metrics({"prediction_slots": 2})
                self.assertEqual(values["pid"], os.getpid())
                self.assertEqual(values["user_cpu_seconds"], 1.25)
                self.assertEqual(values["system_cpu_seconds"], 0.75)
                self.assertEqual(values["peak_rss_bytes"], expected)
                self.assertEqual(values["thread_count"], 7)
                self.assertEqual(values["thread_count_scope"], "synthetic-native")
                self.assertIn("including startup and model loading", values["peak_rss_scope"])
                read.assert_called_once_with(server.resource.RUSAGE_SELF)

    def test_threadpool_metadata_never_exports_library_paths_or_unbounded_fields(self) -> None:
        """Keep optional BLAS details useful without exposing host paths or arbitrary metadata."""
        module = ModuleType("threadpoolctl")
        module.threadpool_info = lambda: [
            {
                "internal_api": "openblas",
                "user_api": "blas",
                "num_threads": 2,
                "version": "0.3.30",
                "filepath": "/private/authored-secret/libblas.dylib",
                "raw_metadata": "authored-secret",
                "architecture": "/private/not-allowed",
            }
        ]
        with patch.dict(sys.modules, {"threadpoolctl": module}):
            available, values = server._threadpool_info()
        self.assertTrue(available)
        self.assertEqual(
            values,
            [
                {
                    "user_api": "blas",
                    "internal_api": "openblas",
                    "version": "0.3.30",
                    "num_threads": 2,
                }
            ],
        )
        self.assertNotIn("authored-secret", json.dumps(values))
        with patch.dict(sys.modules, {"threadpoolctl": None}):
            self.assertEqual(server._threadpool_info(), (False, []))

    def test_numpy_backend_exposes_only_build_identity_without_claiming_runtime_threads(
        self,
    ) -> None:
        """Identify Accelerate without exporting private build paths or inferring active threads."""
        module = ModuleType("numpy")
        module.__version__ = "synthetic-2.0"
        module.__config__ = SimpleNamespace(
            CONFIG={
                "Build Dependencies": {
                    "blas": {
                        "name": "accelerate",
                        "version": "unknown",
                        "found": True,
                        "lib directory": "/private/authored-secret/lib",
                    },
                    "lapack": {
                        "name": "accelerate",
                        "version": "/private/not-a-version",
                        "found": True,
                        "filepath": "/private/authored-secret/lapack",
                    },
                },
                "Compilers": {"path": "/private/authored-secret/compiler"},
            }
        )
        with patch.dict(sys.modules, {"numpy": module}):
            values = server._numpy_backend()
        self.assertTrue(values["available"])
        self.assertEqual(values["numpy_version"], "synthetic-2.0")
        self.assertEqual(
            values["blas"], {"name": "accelerate", "version": "unknown", "found": True}
        )
        self.assertEqual(values["lapack"], {"name": "accelerate", "found": True})
        self.assertFalse(values["runtime_threads_verified"])
        self.assertIn("build configuration only", values["identity_scope"])
        self.assertNotIn("/private", json.dumps(values))
        module.__config__ = SimpleNamespace()
        with patch.dict(sys.modules, {"numpy": module}):
            self.assertNotIn("blas", server._numpy_backend())
        with patch.dict(sys.modules, {"numpy": None}):
            self.assertFalse(server._numpy_backend()["available"])

    def test_control_metrics_endpoint_is_test_only_no_store_and_uses_own_pid(self) -> None:
        """Keep lifecycle counters on the guarded factory and out of ordinary application routes."""
        usage = SimpleNamespace(ru_utime=1.5, ru_stime=0.5, ru_maxrss=1024)
        with patch.dict(os.environ, environment(self.root), clear=True):
            with patch.object(app_v2, "PREDICTIONS_PER_MINUTE", 30):
                with patch.object(
                    app_v2.experiments, "load_selected", return_value=(LOADED, FakeModel())
                ):
                    with patch.object(server.resource, "getrusage", return_value=usage):
                        with patch.object(server, "_thread_count", return_value=(6, "fixture")):
                            with patch.object(server, "_threadpool_info", return_value=(False, [])):
                                app = server.create_app()
                                with TestClient(app) as client:
                                    response = client.get("/__load_test__/metrics")
                                    self.assertEqual(response.status_code, 200)
                                    self.assertEqual(response.headers["cache-control"], "no-store")
                                    metrics = response.json()
                                    self.assertEqual(metrics["pid"], os.getpid())
                                    self.assertEqual(metrics["user_cpu_seconds"], 1.5)
                                    self.assertEqual(metrics["system_cpu_seconds"], 0.5)
                                    self.assertEqual(metrics["thread_count"], 6)
                                    self.assertEqual(metrics["limits"]["prediction_slots"], 2)
                                    self.assertEqual(
                                        metrics["limits"]["production_requests_per_minute"], 30
                                    )
                                    self.assertEqual(
                                        metrics["limits"]["requests_per_minute"], 1_000_000_000
                                    )
                                    self.assertFalse(metrics["threadpool_info_available"])
                                    self.assertIn("numpy_backend", metrics)
                                    self.assertFalse(
                                        metrics["numpy_backend"]["runtime_threads_verified"]
                                    )
                                self.assertNotIn("/__load_test__/metrics", app.openapi()["paths"])
        ordinary = app_v2.create_app(
            root=self.root,
            admin_token="",
            public=True,
            decision_service=DecisionService(DecisionSettings(False, False)),
        )
        self.assertNotIn("/__load_test__/metrics", {route.path for route in ordinary.routes})
        self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)

    def test_public_http_rejects_external_and_private_requests_but_exceeds_thirty_local_calls(
        self,
    ) -> None:
        """Exercise the unchanged API with an authored model while preserving its security gates."""
        with patch.dict(os.environ, environment(self.root), clear=True):
            with patch.object(app_v2, "PREDICTIONS_PER_MINUTE", 30):
                with patch.object(
                    app_v2.experiments, "load_selected", return_value=(LOADED, FakeModel())
                ):
                    with patch.object(
                        DecisionService, "jev", side_effect=AssertionError("No external requests")
                    ):
                        app = server.create_app()
                        with TestClient(app, base_url="https://testserver") as client:
                            config = client.get("/api/config").json()
                            self.assertTrue(config["public"])
                            self.assertFalse(config["operator_configured"])
                            self.assertFalse(config["decision"]["enabled"])
                            self.assertFalse(config["decision"]["server_key_available"])
                            self.assertEqual(client.get("/api/samples").status_code, 403)
                            self.assertIn(client.post("/api/snapshots").status_code, {401, 403})
                            response = client.post(
                                "/api/decision",
                                json={
                                    "text": "Authored meeting reminder.",
                                    "engine": "jev",
                                    "external_consent": True,
                                },
                                headers={"X-TypeSafe-Key": "authored-dummy-key"},
                            )
                            self.assertEqual(response.status_code, 503)
                            for _ in range(35):
                                self.assertEqual(
                                    client.post(
                                        "/api/predict", json={"text": "Authored meeting reminder."}
                                    ).status_code,
                                    200,
                                )
                            self.assertEqual(app.state.load_test_config["prediction_slots"], 2)
        self.assertEqual(app_v2.PREDICTIONS_PER_MINUTE, 30)
        self.assertFalse((self.root / "review.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
