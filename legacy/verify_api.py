"""Check the running local inference API against saved models and its input contract."""

from typing import Any

import hashlib
import json
import math

import httpx
import torch

from legacy import baseline
from textlab import data
from legacy import train_mlp

URL = "http://127.0.0.1:8765"
REPORT = baseline.ARTIFACTS / "api_verification_v1.json"


def snapshot() -> dict[str, str]:
    """Hash protected source files so verification can prove they stayed unchanged."""
    paths = [
        data.DATA / "splits.json",
        data.DATA / "raw" / "SMSSpamCollection",
        baseline.MODEL_PATH,
    ]
    for name in ("mlp_cpu_v1", "mlp_cpu_h16_v1"):
        paths.extend(
            (baseline.ARTIFACTS / name / file)
            for file in ("model.pt", "vectorizer.joblib", "metadata.json")
        )
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def reference_scores() -> dict[str, float]:
    """Score one authored SMS with saved models to check the running local API."""
    train_mlp.configure_cpu()
    text = data.normalize("Free gift")
    bundle, _ = baseline.load_model()
    scores = {"lr": float(baseline.spam_scores(bundle["pipeline"], [text])[0])}
    for model_id, name in (("mlp32", "mlp_cpu_v1"), ("mlp16", "mlp_cpu_h16_v1")):
        model, vectorizer, _ = train_mlp.load_artifact(baseline.ARTIFACTS / name)
        with torch.no_grad():
            scores[model_id] = float(
                torch.sigmoid(model(train_mlp.features(vectorizer, [{"text": text}]))).item()
            )
    return scores


def main() -> None:
    """Run the explicit v1 verification entry point and preserve its report."""
    before = snapshot()
    report = {
        "status": "running",
        "base_url": URL,
        "transport": "real local HTTP",
        "test_set_evaluated": False,
        "checks": [],
    }
    try:
        expected = reference_scores()
        with httpx.Client(base_url=URL, timeout=15, trust_env=False) as client:

            def check(
                name: str, method: str, path: str, status: int, **kwargs: Any
            ) -> httpx.Response:
                """Check an HTTP status before the caller inspects its response body."""
                response = client.request(method, path, **kwargs)
                report["checks"].append(
                    {"name": name, "status": response.status_code, "expected_status": status}
                )
                assert response.status_code == status, (name, response.status_code, response.text)
                return response

            assert check("health", "GET", "/health", 200).json()["ready"] is True
            models = check("model_list", "GET", "/models", 200).json()
            assert {model["id"] for model in models} == {"lr", "mlp32", "mlp16"}
            report["models"] = models
            samples = {}
            for model_id, score in expected.items():
                body = check(
                    f"saved_model_agreement_{model_id}",
                    "POST",
                    "/predict",
                    200,
                    json={"text": "Free gift", "model": model_id},
                ).json()
                assert body["model"] == model_id and body["threshold"] == 0.5
                assert math.isclose(body["spam_score"], score, rel_tol=1e-6, abs_tol=1e-7)
                assert body["label"] == ("spam" if score >= 0.5 else "ham")
                assert body["model_version"]
                samples[model_id] = body
            payload = {"text": "Free gift", "model": "mlp16", "threshold": 0.5}
            normalized = check(
                "normalization_agreement",
                "POST",
                "/predict",
                200,
                json={**payload, "text": "  FREE   gift\n"},
            ).json()
            assert normalized["spam_score"] == samples["mlp16"]["spam_score"]
            for threshold, label in ((0, "spam"), (1, "ham")):
                body = check(
                    f"threshold_{threshold}",
                    "POST",
                    "/predict",
                    200,
                    json={**payload, "threshold": threshold},
                ).json()
                assert body["label"] == label
                assert body["spam_score"] == samples["mlp16"]["spam_score"]
            check(
                "text_at_max_length", "POST", "/predict", 200, json={**payload, "text": "x" * 2000}
            )
            invalid = [
                ("empty_text", {**payload, "text": ""}),
                ("blank_text", {**payload, "text": " \t\n "}),
                ("non_string_text", {**payload, "text": 123}),
                ("missing_text", {"model": "mlp16"}),
                ("missing_model", {"text": "Free gift"}),
                ("unknown_model", {**payload, "model": "missing"}),
                ("threshold_below_range", {**payload, "threshold": -0.1}),
                ("threshold_above_range", {**payload, "threshold": 1.1}),
                ("threshold_string", {**payload, "threshold": "0.5"}),
                ("extra_field", {**payload, "unexpected": True}),
                ("text_over_max_length", {**payload, "text": "x" * 2001}),
            ]
            for name, bad_payload in invalid:
                body = check(name, "POST", "/predict", 422, json=bad_payload).json()
                assert body["detail"] and "spam_score" not in body
                assert all("input" not in error for error in body["detail"])
                if name == "blank_text":
                    report["blank_text_response"] = body
            for token in ("NaN", "Infinity", "-Infinity"):
                body = '{"text":"Free gift","model":"mlp16","threshold":' + token + "}"
                check(
                    f"non_finite_{token}",
                    "POST",
                    "/predict",
                    422,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
            check(
                "malformed_json",
                "POST",
                "/predict",
                422,
                content="{",
                headers={"Content-Type": "application/json"},
            )
            check("wrong_http_method", "GET", "/predict", 405)
            check("openapi_schema", "GET", "/openapi.json", 200)
            assert snapshot() == before
            report.update(
                status="passed",
                artifacts_unchanged=True,
                sample_predictions=samples,
                check_count=len(report["checks"]),
            )
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        data.save_json(REPORT, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": report["check_count"],
                "sample": report["sample_predictions"]["mlp16"],
                "report": str(REPORT),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
