"""Train a text baseline, inspect validation only, and preserve the held-out test set."""

from typing import Any

import argparse
from collections import Counter
import importlib.metadata
import json
import time
import warnings

import joblib
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score
from sklearn.pipeline import Pipeline

from textlab import data

MAX_FEATURES = 2000
LR_MAX_ITERATIONS = 1000
DEFAULT_THRESHOLD = 0.5
COEFFICIENT_TOLERANCE = 1e-12
EXPLANATION_TERM_LIMIT = 5
ERROR_EXAMPLE_LIMIT = 3


ARTIFACTS = data.ROOT / "artifacts"
MODEL_PATH = ARTIFACTS / "baseline.joblib"


def train() -> None:
    """Fit the v1 baseline on training rows only to preserve hold-out evidence."""
    prepared, split_sha = data.load_splits()
    rows = prepared["splits"]["train"]
    pipeline = Pipeline(
        [
            ("tfidf", TfidfVectorizer(max_features=MAX_FEATURES, lowercase=False)),
            ("model", LogisticRegression(max_iter=LR_MAX_ITERATIONS, random_state=data.SEED)),
        ]
    )
    started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        # Both vocabulary/IDF and classifier weights learn from training rows only.
        pipeline.fit([row["text"] for row in rows], [row["label"] for row in rows])
    bundle = {
        "pipeline": pipeline,
        "split_sha256": split_sha,
        "training_rows": len(rows),
        "training_seconds": time.perf_counter() - started,
        "seed": data.SEED,
        "normalization": prepared["manifest"]["normalization"],
        "majority_label": Counter(row["label"] for row in rows).most_common(1)[0][0],
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("scikit-learn", "numpy", "scipy", "joblib")
        },
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, MODEL_PATH)
    print(json.dumps({key: value for key, value in bundle.items() if key != "pipeline"}, indent=2))


def load_model() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the local baseline and verify its frozen data provenance."""
    prepared, split_sha = data.load_splits()
    bundle = joblib.load(MODEL_PATH)  # Only load this project's locally generated artifact.
    if bundle["split_sha256"] != split_sha:
        raise ValueError("Model and frozen data splits do not match")
    return bundle, prepared


def spam_scores(pipeline: Pipeline, texts: list[str]) -> np.ndarray:
    """Read spam probabilities from the saved class ordering for consistent inference."""
    index = list(pipeline.classes_).index("spam")
    return pipeline.predict_proba(texts)[:, index]


def metrics(labels: list[str], predicted: list[str] | np.ndarray) -> dict[str, Any]:
    """Summarize classification counts and scores with explicit undefined-value policy."""
    tn, fp, fn, tp = (
        int(value) for value in confusion_matrix(labels, predicted, labels=["ham", "spam"]).ravel()
    )
    return {
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "true_positive": tp,
        "spam_precision": tp / (tp + fp) if tp + fp else None,
        "spam_recall": tp / (tp + fn) if tp + fn else None,
        "spam_f1": float(f1_score(labels, predicted, pos_label="spam", zero_division=0)),
        "macro_f1": float(
            f1_score(labels, predicted, labels=["ham", "spam"], average="macro", zero_division=0)
        ),
        "ham_precision": tn / (tn + fn) if tn + fn else None,
        "ham_recall": tn / (tn + fp) if tn + fp else None,
        "accuracy": float(accuracy_score(labels, predicted)),
    }


def explain(pipeline: Pipeline, text: str) -> dict[str, Any]:
    """Expose token contributions to explain a fixed baseline prediction."""
    vectorizer, classifier = pipeline["tfidf"], pipeline["model"]
    if list(classifier.classes_) != ["ham", "spam"]:
        raise ValueError("Unexpected coefficient/class ordering")
    vector = vectorizer.transform([data.normalize(text)])
    names = vectorizer.get_feature_names_out()
    parts = [
        {
            "term": str(names[index]),
            "tfidf": float(weight),
            "coefficient": float(classifier.coef_[0, index]),
            "contribution": float(weight * classifier.coef_[0, index]),
        }
        for index, weight in zip(vector.indices, vector.data)
    ]
    logit = float(classifier.intercept_[0] + sum(part["contribution"] for part in parts))
    np.testing.assert_allclose(
        logit,
        classifier.decision_function(vector)[0],
        rtol=COEFFICIENT_TOLERANCE,
        atol=COEFFICIENT_TOLERANCE,
    )
    return {
        "bias": float(classifier.intercept_[0]),
        "logit": logit,
        "positive_terms": sorted(
            [part for part in parts if part["contribution"] > 0],
            key=lambda part: -part["contribution"],
        )[:EXPLANATION_TERM_LIMIT],
        "negative_terms": sorted(
            [part for part in parts if part["contribution"] < 0],
            key=lambda part: part["contribution"],
        )[:EXPLANATION_TERM_LIMIT],
    }


def validate(thresholds: list[float]) -> None:
    """Compare thresholds on validation rows without fitting or touching the test set."""
    bundle, prepared = load_model()
    pipeline = bundle["pipeline"]
    rows = prepared["splits"]["validation"]
    texts, labels = [row["text"] for row in rows], [row["label"] for row in rows]
    vectorizer = pipeline["tfidf"]
    vocabulary_before = dict(vectorizer.vocabulary_)
    idf_before = vectorizer.idf_.copy()
    scores = spam_scores(pipeline, texts)
    threshold_reports = []
    for threshold in thresholds:
        predicted = np.where(scores >= threshold, "spam", "ham")
        examples = {}
        for kind, expected, actual in (
            ("false_positive", "ham", "spam"),
            ("false_negative", "spam", "ham"),
        ):
            mistakes = [
                (row, float(score))
                for row, score, pred in zip(rows, scores, predicted)
                if row["label"] == expected and pred == actual
            ]
            # Prefer errors nearest the threshold, useful for explaining the decision.
            mistakes.sort(key=lambda pair: abs(pair[1] - threshold))
            examples[kind] = [
                {
                    "id": row["id"],
                    "text": row["text"],
                    "expected": expected,
                    "predicted": actual,
                    "spam_score": score,
                    "explanation": explain(pipeline, row["text"]),
                }
                for row, score in mistakes[:ERROR_EXAMPLE_LIMIT]
            ]
        threshold_reports.append(
            {"threshold": threshold, **metrics(labels, predicted), "examples": examples}
        )
    # Inference must neither refit the vocabulary nor update IDF.
    assert vectorizer.vocabulary_ == vocabulary_before
    np.testing.assert_array_equal(vectorizer.idf_, idf_before)
    report = {
        "evaluation_split": "validation",
        "test_set_evaluated": False,
        "threshold_selected": None,
        "positive_class": "spam",
        "sample_count": len(rows),
        "class_counts": dict(Counter(labels)),
        "data_manifest": prepared["manifest"],
        "training": {key: value for key, value in bundle.items() if key != "pipeline"},
        "f1_zero_division_policy": "0 if undefined; undefined precision/recall are null",
        "majority_baseline": {
            "predicted_label": bundle["majority_label"],
            **metrics(labels, [bundle["majority_label"]] * len(rows)),
        },
        "thresholds": threshold_reports,
        "limitations": [
            "English SMS teaching dataset, not evidence of suitability for Chinese or live traffic",
            "Normalized exact repeats removed; near-duplicates/templates not checked",
            "Validation results are used for development, not final generalization claims",
        ],
    }
    data.save_json(ARTIFACTS / "validation_report.json", report)
    for item in threshold_reports:
        print(json.dumps({key: value for key, value in item.items() if key != "examples"}))


def predict(text: str, threshold: float) -> None:
    """Print one baseline prediction with its saved-model explanation."""
    bundle, _ = load_model()
    normalized = data.normalize(text)
    if not normalized:
        raise ValueError("Text must not be blank")
    score = float(spam_scores(bundle["pipeline"], [normalized])[0])
    print(
        json.dumps(
            {
                "text": text,
                "spam_score": score,
                "threshold": threshold,
                "label": "spam" if score >= threshold else "ham",
                "explanation": explain(bundle["pipeline"], normalized),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def threshold(value: str) -> float:
    """Parse a finite probability threshold so CLI inputs stay valid."""
    number = float(value)
    if not np.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("Threshold must be a finite value between 0 and 1")
    return number


def main() -> None:
    """Dispatch explicit v1 CLI operations without running them on import."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("download", "prepare", "train"):
        commands.add_parser(name)
    validation = commands.add_parser("validate")
    validation.add_argument(
        "--thresholds", nargs="+", type=threshold, default=[DEFAULT_THRESHOLD, 0.8, 0.9]
    )
    prediction = commands.add_parser("predict")
    prediction.add_argument("text")
    prediction.add_argument("--threshold", type=threshold, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()
    if args.command == "download":
        data.download()
        print("Official source archive and readme preserved.")
    elif args.command == "prepare":
        print(json.dumps(data.prepare(), indent=2))
    elif args.command == "train":
        train()
    elif args.command == "validate":
        validate(args.thresholds)
    elif args.command == "predict":
        predict(args.text, args.threshold)


if __name__ == "__main__":
    main()
