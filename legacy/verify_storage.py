"""Exercise sample APIs with real SQLite in a disposable copy, never edit the working DB."""

from typing import Any

import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from legacy import app as api
from legacy import baseline
from textlab import data

REPORT = baseline.ARTIFACTS / "storage_verification_v1.json"


def snapshot() -> Any:
    """Hash protected source files so verification can prove they stayed unchanged."""
    paths = [
        data.DATA / "splits.json",
        data.DATA / "raw" / "SMSSpamCollection",
        baseline.MODEL_PATH,
    ]
    for run in ("mlp_cpu_v1", "mlp_cpu_h16_v1"):
        paths.extend(
            baseline.ARTIFACTS / run / name
            for name in ("model.pt", "vectorizer.joblib", "metadata.json")
        )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def stored_rows(path: Path) -> Any:
    """Read stored labels and revisions to verify transaction behavior."""
    with sqlite3.connect(path) as connection:
        return {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT id, text, original_label, label, revision FROM samples"
            )
        }


def main() -> None:
    """Run the explicit v1 verification entry point and preserve its report."""
    original_path, before = api.DATABASE_PATH, snapshot()
    report = {
        "status": "running",
        "transport": "in-process HTTP through FastAPI TestClient",
        "database": "disposable real SQLite, seeded from train only",
        "real_working_labels_modified": False,
        "test_set_evaluated": False,
        "checks": [],
    }
    try:
        with TemporaryDirectory(prefix="sms-storage-verification-") as temporary:
            path = Path(temporary) / "samples.sqlite3"
            api.DATABASE_PATH = path
            with TestClient(api.app) as client:

                def check(
                    name: Any, method: Any, route: Any, status: Any = 200, **kwargs: Any
                ) -> Any:
                    """Check the expected failure to verify the storage boundary."""
                    response = client.request(method, route, **kwargs)
                    assert response.status_code == status, (
                        name,
                        response.status_code,
                        response.text,
                    )
                    report["checks"].append(name)
                    return response.json()

                page = check("training_copy_seeded", "GET", "/samples", params={"limit": 2})
                assert page["total"] == 3611 and len(page["items"]) == 2
                first, other = page["items"]
                identity, old_label = first["id"], first["label"]
                new_label = "spam" if old_label == "ham" else "ham"
                route = f"/samples/{identity}"
                before_rows = stored_rows(path)
                empty_history = check("initial_history_empty", "GET", route + "/corrections")
                assert empty_history == []
                prediction = {"text": first["text"], "model": "mlp16", "threshold": 0.5}
                prediction_before = check(
                    "prediction_before_correction", "POST", "/predict", json=prediction
                )
                result = check(
                    "label_and_history_saved",
                    "PATCH",
                    route + "/label",
                    json={"label": new_label, "note": "isolated verification"},
                )
                assert result["changed"] and result["sample"]["revision"] == 1
                assert result["sample"]["original_label"] == old_label
                reloaded = check("separate_connection_reads_change", "GET", route)
                assert reloaded["label"] == new_label
                after_rows = stored_rows(path)
                assert {key for key in before_rows if before_rows[key] != after_rows[key]} == {
                    identity
                }
                assert before_rows[other["id"]] == after_rows[other["id"]]
                report["checks"].append("exactly_one_sample_changed")
                history = check("history_records_old_and_new", "GET", route + "/corrections")
                assert (
                    len(history) == 1
                    and history[0]["old_label"] == old_label
                    and history[0]["new_label"] == new_label
                )
                repeated = check(
                    "same_label_is_idempotent", "PATCH", route + "/label", json={"label": new_label}
                )
                assert not repeated["changed"] and repeated["sample"]["revision"] == 1
                prediction_after = check(
                    "prediction_after_correction", "POST", "/predict", json=prediction
                )
                assert prediction_before == prediction_after
                report["checks"].append("saving_labels_does_not_retrain_model")
                query = "' OR 1=1 --"
                searched = check(
                    "search_is_literal_parameter", "GET", "/samples", params={"q": query}
                )
                assert all(query in row["text"] for row in searched["items"])
                filtered = check(
                    "label_filter_and_pagination",
                    "GET",
                    "/samples",
                    params={"label": new_label, "limit": 2, "offset": 0},
                )
                assert all(row["label"] == new_label for row in filtered["items"])
                for name, params in (
                    ("limit_zero", {"limit": 0}),
                    ("limit_over_max", {"limit": 101}),
                    ("negative_offset", {"offset": -1}),
                    ("invalid_filter", {"label": "other"}),
                ):
                    check(name, "GET", "/samples", 422, params=params)
                check("invalid_label", "PATCH", route + "/label", 422, json={"label": "other"})
                check(
                    "unknown_update_field",
                    "PATCH",
                    route + "/label",
                    422,
                    json={"label": old_label, "unexpected": True},
                )
                check("missing_sample_get", "GET", "/samples/not-present", 404)
                check(
                    "missing_sample_patch",
                    "PATCH",
                    "/samples/not-present/label",
                    404,
                    json={"label": "ham"},
                )
                check("missing_sample_history", "GET", "/samples/not-present/corrections", 404)
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "CREATE TRIGGER verification_abort BEFORE INSERT ON corrections "
                        "BEGIN SELECT RAISE(ABORT, 'injected verification failure'); END"
                    )
                failing_label = "spam" if other["label"] == "ham" else "ham"
                check(
                    "history_failure_returns_500",
                    "PATCH",
                    f"/samples/{other['id']}/label",
                    500,
                    json={"label": failing_label},
                )
                assert stored_rows(path) == after_rows
                report["checks"].append("transaction_failure_rolls_back_entire_change")
                with sqlite3.connect(path) as connection:
                    connection.execute("DROP TRIGGER verification_abort")
            # A fresh app lifespan initializes the same DB without overwriting corrections.
            with TestClient(api.app) as client:
                restarted = client.get(route)
                assert restarted.status_code == 200 and restarted.json()["label"] == new_label
                assert restarted.json()["revision"] == 1
                assert len(client.get(route + "/corrections").json()) == 1
                report["checks"].append("restart_preserves_label_and_history")
            assert snapshot() == before
            report.update(
                status="passed",
                check_count=len(report["checks"]),
                frozen_data_and_models_unchanged=True,
                training_copy_rows=3611,
            )
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        api.DATABASE_PATH = original_path
        data.save_json(REPORT, report)
    print(
        json.dumps(
            {"status": report["status"], "checks": report["check_count"], "report": str(REPORT)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
