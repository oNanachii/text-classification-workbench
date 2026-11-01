"""Check private data flow, restart activation and public read-only mode over HTTP."""

from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import httpx

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from textlab import experiments  # noqa: E402

HTTP_TIMEOUT_SECONDS = 10
SERVER_START_ATTEMPTS = 100
SERVER_POLL_SECONDS = 0.05
PROCESS_STOP_TIMEOUT_SECONDS = 10
DEFAULT_THRESHOLD = 0.5
TIMING_REQUEST_COUNT = 20
MILLISECONDS_PER_SECOND = 1000
TIMING_DISPLAY_DIGITS = 3


TOKEN = "isolated-http-test-token"


class Server:
    def __init__(self, root: Path, public: bool = False) -> None:
        """Start a disposable loopback server so integration checks use real HTTP."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = {
            **os.environ,
            "SMS_LAB_ROOT": str(root),
            "SMS_LAB_ADMIN_TOKEN": TOKEN,
            "SMS_LAB_PUBLIC": "true" if public else "false",
        }
        self.p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "textlab.app_v2:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--no-access-log",
            ],
            cwd=PROJECT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=HTTP_TIMEOUT_SECONDS
        )
        for _ in range(SERVER_START_ATTEMPTS):
            try:
                self.client.get("/api/health").raise_for_status()
                break
            except (httpx.HTTPError, ConnectionError):
                if self.p.poll() is not None:
                    raise RuntimeError("Integration server exited")
                time.sleep(SERVER_POLL_SECONDS)
        else:
            raise RuntimeError("Integration server did not become ready")

    def close(self) -> None:
        """Release the client and subprocess so integration checks leave no server behind."""
        self.client.close()
        self.p.terminate()
        try:
            self.p.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


def main() -> None:
    """Verify the v2 workflow on disposable copies without changing formal evidence."""
    result = {
        "scope": (
            "actual localhost HTTP, disposable v2 database and artifact copies, "
            "real numerical models"
        ),
        "original_data_modified": False,
        "test_set_evaluated": False,
        "model_api_called": False,
    }
    with tempfile.TemporaryDirectory(prefix="sms-v2-http-") as temp:
        root = Path(temp) / "local"
        shutil.copytree(
            PROJECT / "var",
            root,
            ignore=shutil.ignore_patterns(
                "operator-token.txt",
                "final-plan.json",
                "final-attempt.json",
                "final-test-report.json",
            ),
        )
        experiments.select(root, "v2-base-seed42", "lr", DEFAULT_THRESHOLD)
        server = Server(root)
        try:
            c = server.client
            auth = {"Authorization": "Bearer " + TOKEN}
            assert c.get("/api/samples").status_code == 401
            assert (
                c.post(
                    "/api/imports/preview",
                    json={"csv": "text,label\nprivate content example,ham", "source": "fixture"},
                ).status_code
                == 401
            )
            before = c.post("/api/snapshots", headers=auth).raise_for_status().json()
            p = (
                c.post(
                    "/api/imports/preview",
                    headers=auth,
                    json={
                        "csv": (
                            "text,label\nplease deliver the orange workbook "
                            "to the studio next monday,ham\n"
                        ),
                        "source": (
                            "independently authored HTTP workflow fixture, not a real correction"
                        ),
                    },
                )
                .raise_for_status()
                .json()
            )
            assert p["summary"]["accepted_rows"] == 1
            body = {"preview_id": p["preview_id"]}
            a = c.post("/api/imports/confirm", headers=auth, json=body).raise_for_status().json()
            b = c.post("/api/imports/confirm", headers=auth, json=body).raise_for_status().json()
            assert a["created"] and not b["created"]
            snap_pending = c.post("/api/snapshots", headers=auth).raise_for_status().json()
            assert snap_pending["snapshot_id"] == before["snapshot_id"]
            rows = (
                c.get("/api/samples?pending=true", headers=auth).raise_for_status().json()["items"]
            )
            row = next(r for r in rows if r["id"] == p["sample_preview"][0]["id"])
            reviewed = (
                c.patch(
                    "/api/samples/" + row["id"],
                    headers=auth,
                    json={
                        "label": "ham",
                        "note": "literal work reminder; internal fixture review",
                        "expected_revision": row["revision"],
                    },
                )
                .raise_for_status()
                .json()
            )
            assert not reviewed["changed"]
            after = c.post("/api/snapshots", headers=auth).raise_for_status().json()
            assert after["snapshot_id"] != before["snapshot_id"]
            svc = c.get("/api/service").json()
            c.post(
                "/api/service/select",
                headers=auth,
                json={
                    "run_id": "v2-import-reviewed-seed42",
                    "kind": "mlp16",
                    "threshold": DEFAULT_THRESHOLD,
                    "expected_revision": svc["revision"],
                },
            ).raise_for_status()
            assert c.get("/api/service").json()["restart_required"]
            assert (
                c.post("/api/predict", json={"text": "normal meeting reminder"})
                .raise_for_status()
                .json()["model"]
                == "lr"
            )
            result["import_review_snapshot"] = {
                "accepted": 1,
                "confirmation_idempotent": True,
                "pending_excluded": True,
                "same_label_review_not_fake_correction": True,
                "snapshot_changes_after_review": True,
            }
        finally:
            server.close()
        server = Server(root)
        try:
            c = server.client
            assert c.get("/api/health").json()["loaded"]["run_id"] == "v2-import-reviewed-seed42"
            svc = c.get("/api/service").json()
            c.post(
                "/api/service/rollback", headers=auth, json={"expected_revision": svc["revision"]}
            ).raise_for_status()
        finally:
            server.close()
        server = Server(root)
        try:
            assert server.client.get("/api/health").json()["loaded"]["run_id"] == "v2-base-seed42"
        finally:
            server.close()
        result["selection_restart_rollback"] = {
            "selected_does_not_fake_loaded": True,
            "new_process_loads_candidate": True,
            "rollback_restores_previous_run": True,
        }
        exported = Path(temp) / "public"
        experiments.export_public(root, exported)
        server = Server(exported, True)
        try:
            c = server.client
            assert c.get("/api/samples").status_code == 403
            assert c.get("/api/samples", headers=auth).status_code == 403
            assert c.post("/api/snapshots", headers=auth).status_code == 403
            assert len(c.get("/api/runs").raise_for_status().json()["runs"]) == 1
            assert c.get("/workbench/").status_code == 200
            times = []
            for _ in range(TIMING_REQUEST_COUNT):
                t = time.perf_counter()
                r = c.post(
                    "/api/predict",
                    json={"text": "could you bring the notebook to our project meeting tomorrow"},
                ).raise_for_status()
                times.append((time.perf_counter() - t) * MILLISECONDS_PER_SECOND)
                assert r.json()["model_version"] == "v2-base-seed42"
            assert r.headers["cache-control"] == "no-store"
            assert "script-src 'self'" in r.headers["content-security-policy"]
            result["public"] = {
                "readonly_even_with_token": True,
                "private_rows_not_exported": not (exported / "review.sqlite3").exists(),
                "selected_run_only": True,
                "frontend_http": 200,
                "actual_inference": True,
                "scope": "20 warmed localhost requests, not capacity or public latency benchmark",
                "median_ms": round(sorted(times)[TIMING_REQUEST_COUNT // 2], TIMING_DISPLAY_DIGITS),
                "max_ms": round(max(times), TIMING_DISPLAY_DIGITS),
                "observed_process_rss_kib": int(
                    subprocess.check_output(
                        ["ps", "-p", str(server.p.pid), "-o", "rss="], text=True
                    ).strip()
                ),
            }
        finally:
            server.close()
    result["status"] = "passed"
    (PROJECT / "artifacts/v2-verification/http-integration.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
