"""CPU MLP teaching experiment; only training and validation rows are used."""

import argparse
from collections import Counter
import importlib.metadata
import json
import math
from pathlib import Path
import random
import re
import time

import joblib
import numpy as np
import torch
from torch import nn
from threadpoolctl import threadpool_limits

import baseline
import data


DEFAULT_RUN = "mlp_cpu_v1"
DEFAULTS = {"input_dim": 2000, "hidden_units": 32, "batch_size": 64,
            "learning_rate": 0.001, "epochs": 20, "seed": 42, "cpu_threads": 2,
            "device": "cpu", "dtype": "float32", "optimizer": "Adam",
            "loss": "BCEWithLogitsLoss", "activation": "ReLU", "weight_decay": 0.0}


def configure_cpu():
    torch.set_num_threads(2)
    if torch.get_num_interop_threads() > 2:
        torch.set_num_interop_threads(2)
    # Cap numerical-library thread pools used by the reused sklearn baseline too.
    threadpool_limits(limits=2)


def run_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise argparse.ArgumentTypeError("Run name must use 1-80 letters, digits, underscores or hyphens")
    return value


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer")
    return number


def make_model(config):
    return nn.Sequential(nn.Linear(config["input_dim"], config["hidden_units"]),
                         nn.ReLU(), nn.Linear(config["hidden_units"], 1)).cpu()


def finite(tensor, name):
    if not bool(torch.isfinite(tensor).all()):
        raise FloatingPointError(f"Non-finite {name}; experiment stopped")


def norm(tensors):
    return math.sqrt(sum(float(tensor.detach().double().square().sum()) for tensor in tensors))


def features(vectorizer, rows):
    return torch.from_numpy(vectorizer.transform([row["text"] for row in rows]).astype(np.float32).toarray())


def labels_tensor(rows):
    return torch.tensor([float(row["label"] == "spam") for row in rows], dtype=torch.float32)


def train_step(model, optimizer, criterion, x_batch, y_batch, audit=None):
    parameters = list(model.parameters())
    before = None
    if audit is not None:
        before = [parameter.detach().clone() for parameter in parameters]
        # Deliberately seed old gradients to prove zero_grad clears them.
        for parameter in parameters:
            parameter.grad = torch.ones_like(parameter)
        audit["old_gradients_seeded_for_audit"] = True
        audit["old_gradient_norm"] = norm(parameter.grad for parameter in parameters)

    optimizer.zero_grad()                       # 1. Clear previous gradients.
    if audit is not None:
        audit["zero_grad_cleared_old_gradients"] = all(
            parameter.grad is None or not bool(parameter.grad.count_nonzero()) for parameter in parameters)
        assert audit["zero_grad_cleared_old_gradients"]
    logits = model(x_batch).squeeze(-1)          # 2. Forward: raw logits, not sigmoid probabilities.
    loss = criterion(logits, y_batch)           # 3. BCEWithLogitsLoss includes the stable sigmoid calculation.
    finite(loss, "training loss")
    loss.backward()                             # 4. Calculate gradients; weights are unchanged here.
    for parameter in parameters:
        if parameter.grad is not None:
            finite(parameter.grad, "gradient")
    if audit is not None:
        audit["gradients_finite"] = True
        audit["gradient_norm_after_backward"] = norm(
            parameter.grad for parameter in parameters if parameter.grad is not None)
        audit["backward_kept_parameters_unchanged"] = all(
            torch.equal(parameter.detach(), old) for parameter, old in zip(parameters, before))
        assert audit["gradient_norm_after_backward"] > 0
        assert audit["backward_kept_parameters_unchanged"]
    optimizer.step()                            # 5. Update weights using the gradients.
    for parameter in parameters:
        finite(parameter, "updated parameter")
    if audit is not None:
        audit["parameter_change_norm_after_step"] = norm(
            parameter.detach() - old for parameter, old in zip(parameters, before))
        audit["shapes"] = {"input": list(x_batch.shape), "logits": list(logits.shape),
                           "labels": list(y_batch.shape), "parameters": {
                               name: list(parameter.shape) for name, parameter in model.named_parameters()}}
        assert audit["parameter_change_norm_after_step"] > 0
        audit["passed"] = True


def evaluate(model, x, y, criterion, batch_size):
    """Evaluate one fixed epoch-end model; never average changing training-step losses."""
    model.eval()
    total_loss, scores = 0.0, []
    with torch.no_grad():
        for start in range(0, len(x), batch_size):
            logits = model(x[start:start + batch_size]).squeeze(-1)
            loss = criterion(logits, y[start:start + batch_size])
            finite(loss, "evaluation loss")
            probabilities = torch.sigmoid(logits)
            finite(probabilities, "evaluation probabilities")
            total_loss += float(loss) * len(logits)
            scores.append(probabilities.numpy())
    return total_loss / len(x), np.concatenate(scores)


def assert_vectorizer_unchanged(vectorizer, vocabulary, idf):
    assert vectorizer.vocabulary_ == vocabulary, "Vocabulary changed"
    np.testing.assert_array_equal(vectorizer.idf_, idf)


def file_sha(path):
    return data.digest(path.read_bytes())


def load_artifact(directory):
    """Load only this project's locally generated artifacts, never untrusted joblib files."""
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    weights_path, vectorizer_path = directory / "model.pt", directory / "vectorizer.joblib"
    if file_sha(weights_path) != metadata["model_file_sha256"]:
        raise ValueError("Saved model checksum mismatch")
    if file_sha(vectorizer_path) != metadata["vectorizer_file_sha256"]:
        raise ValueError("Saved vectorizer checksum mismatch")
    checkpoint = torch.load(weights_path, weights_only=True, map_location="cpu")
    for key in ("config", "epoch", "seed", "split_sha256", "raw_sha256", "vectorizer_file_sha256"):
        if checkpoint[key] != metadata[key]:
            raise ValueError(f"Checkpoint metadata mismatch: {key}")
    model = make_model(checkpoint["config"])
    model.load_state_dict(checkpoint["state_dict"], strict=True)
    model.eval()
    vectorizer = joblib.load(vectorizer_path)
    if len(vectorizer.vocabulary_) != checkpoint["config"]["input_dim"]:
        raise ValueError("Vectorizer dimension does not match model")
    return model, vectorizer, metadata


def save_best(directory, model, vectorizer, report, best, vocabulary, idf, validation_rows, x_val, y_val):
    model.load_state_dict(best["state_dict"])
    model.eval()
    _, before_scores = evaluate(model, x_val, y_val, nn.BCEWithLogitsLoss(), report["config"]["batch_size"])
    assert_vectorizer_unchanged(vectorizer, vocabulary, idf)
    vectorizer_path = directory / "vectorizer.joblib"
    joblib.dump(vectorizer, vectorizer_path)
    metadata = {"config": report["config"], "epoch": best["epoch"], "seed": report["config"]["seed"],
                "split_sha256": report["data"]["split_sha256"], "raw_sha256": report["data"]["raw_sha256"],
                "vectorizer_file_sha256": file_sha(vectorizer_path),
                "normalization": report["data"]["normalization"], "label_mapping": {"ham": 0, "spam": 1},
                "best_validation_loss": best["validation_loss"], "versions": report["versions"]}
    torch.save({**{key: metadata[key] for key in (
        "config", "epoch", "seed", "split_sha256", "raw_sha256", "vectorizer_file_sha256")},
        "state_dict": best["state_dict"]}, directory / "model.pt")
    metadata["model_file_sha256"] = file_sha(directory / "model.pt")
    data.save_json(directory / "metadata.json", metadata)

    restored, restored_vectorizer, _ = load_artifact(directory)
    assert_vectorizer_unchanged(restored_vectorizer, vocabulary, idf)
    # Re-transform validation rows to verify the saved vectorizer as well as the weights.
    restored_x = features(restored_vectorizer, validation_rows)
    _, restored_scores = evaluate(restored, restored_x, y_val, nn.BCEWithLogitsLoss(), report["config"]["batch_size"])
    np.testing.assert_allclose(restored_scores, before_scores, rtol=1e-6, atol=1e-7)
    np.testing.assert_array_equal(restored_scores >= 0.5, before_scores >= 0.5)
    assert_vectorizer_unchanged(vectorizer, vocabulary, idf)
    report["reload_check"] = {
        "passed": True, "same_batch_size": report["config"]["batch_size"],
        "rtol": 1e-6, "atol": 1e-7, "maximum_score_difference": float(np.max(np.abs(restored_scores - before_scores))),
        "labels_at_0_5_identical": True, "vocabulary_unchanged": True, "idf_unchanged": True}
    report["best_checkpoint"] = metadata
    report["comparison_at_threshold_0_5"]["mlp"] = {
        "epoch": best["epoch"], "validation_loss": best["validation_loss"],
        **baseline.metrics([row["label"] for row in validation_rows], np.where(restored_scores >= 0.5, "spam", "ham"))}


def train(name, epochs, hidden_units=DEFAULTS["hidden_units"]):
    directory = baseline.ARTIFACTS / run_name(name)
    directory.mkdir(parents=True, exist_ok=False)  # Never replace an earlier run.
    config = {**DEFAULTS, "epochs": epochs, "hidden_units": hidden_units}
    report = {"status": "running", "config": config, "evaluation_split": "validation",
              "positive_class": "spam", "test_set_evaluated": False, "threshold_selected": None,
              "checkpoint_selection": "minimum epoch-end validation BCEWithLogitsLoss",
              "history": [], "first_step_audit": {},
              "limitations": ["Near-duplicates and shared templates have not been checked",
                              "English SMS teaching data; no Chinese or live-traffic acceptance",
                              "Validation is used for checkpoint selection, not final generalization claims",
                              "Seeded CPU results are not guaranteed identical across library versions or machines",
                              "Checkpoint stores inference weights, not optimizer state for exact training resumption"],
              "f1_zero_division_policy": "0 if undefined; undefined precision/recall are null"}
    started = time.perf_counter()
    try:
        configure_cpu()
        random.seed(config["seed"])
        np.random.seed(config["seed"])
        torch.manual_seed(config["seed"])
        torch.use_deterministic_algorithms(True)
        report["versions"] = {name: importlib.metadata.version(name) for name in (
            "torch", "numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl")}
        report["cpu_threads"] = {"intra_op": torch.get_num_threads(), "inter_op": torch.get_num_interop_threads(),
                                 "numeric_library_limit": 2}
        report["implementation_sha256"] = file_sha(Path(__file__).resolve())
        bundle, prepared = baseline.load_model()
        # The existing loader reads the frozen JSON; no test rows are selected, transformed or scored here.
        train_rows = prepared["splits"]["train"]
        val_rows = prepared["splits"]["validation"]
        manifest = prepared["manifest"]
        del prepared
        vectorizer = bundle["pipeline"]["tfidf"]
        vocabulary, idf = dict(vectorizer.vocabulary_), vectorizer.idf_.copy()
        if len(vocabulary) != config["input_dim"]:
            raise ValueError("The frozen baseline must provide exactly 2000 TF-IDF features")
        report["data"] = {"split_sha256": bundle["split_sha256"], "raw_sha256": manifest["raw_sha256"],
                          "normalization": manifest["normalization"], "near_duplicate_check": manifest["near_duplicate_check"],
                          "train_count": len(train_rows), "validation_count": len(val_rows),
                          "train_class_counts": dict(Counter(row["label"] for row in train_rows)),
                          "validation_class_counts": dict(Counter(row["label"] for row in val_rows)),
                          "vectorizer_fit_split": "train (reused frozen baseline)",
                          "source_baseline_file_sha256": file_sha(baseline.MODEL_PATH)}
        x_train, x_val = features(vectorizer, train_rows), features(vectorizer, val_rows)
        y_train, y_val = labels_tensor(train_rows), labels_tensor(val_rows)
        report["feature_shapes"] = {"train": list(x_train.shape), "validation": list(x_val.shape)}
        report["dense_feature_bytes"] = (x_train.numel() + x_val.numel()) * x_train.element_size()
        lr_scores = baseline.spam_scores(bundle["pipeline"], [row["text"] for row in val_rows])
        report["comparison_at_threshold_0_5"] = {
            "split": "validation", "threshold": 0.5, "same_validation_rows": True,
            "logistic_regression": baseline.metrics(
                [row["label"] for row in val_rows], np.where(lr_scores >= 0.5, "spam", "ham"))}

        model = make_model(config)
        criterion = nn.BCEWithLogitsLoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=config["learning_rate"])
        generator = torch.Generator().manual_seed(config["seed"])
        best = None
        for epoch in range(1, epochs + 1):
            epoch_started = time.perf_counter()
            model.train()
            order = torch.randperm(len(x_train), generator=generator)
            for start in range(0, len(order), config["batch_size"]):
                indices = order[start:start + config["batch_size"]]
                audit = report["first_step_audit"] if epoch == 1 and start == 0 else None
                train_step(model, optimizer, criterion, x_train[indices], y_train[indices], audit)
            train_loss, _ = evaluate(model, x_train, y_train, criterion, config["batch_size"])
            val_loss, val_scores = evaluate(model, x_val, y_val, criterion, config["batch_size"])
            result = {"epoch": epoch, "train_loss": train_loss, "validation_loss": val_loss,
                      "threshold": 0.5, "epoch_seconds": time.perf_counter() - epoch_started,
                      **baseline.metrics([row["label"] for row in val_rows], np.where(val_scores >= 0.5, "spam", "ham"))}
            report["history"].append(result)
            if best is None or val_loss < best["validation_loss"]:
                best = {"epoch": epoch, "validation_loss": val_loss,
                        "state_dict": {name: tensor.detach().cpu().clone() for name, tensor in model.state_dict().items()}}
            print(json.dumps(result), flush=True)
            if epoch == 1:
                remaining = result["epoch_seconds"] * (epochs - 1)
                report["time_estimate"] = {"first_epoch_seconds": result["epoch_seconds"],
                                           "estimated_remaining_seconds": remaining, "remaining_limit_seconds": 60}
                if remaining > 60:
                    report["status"] = "stopped_time_budget"
                    report["stop_reason"] = "First-epoch estimate exceeds 60 seconds remaining; experiment settings were not changed"
                    break
        save_best(directory, model, vectorizer, report, best, vocabulary, idf, val_rows, x_val, y_val)
        if report["status"] == "running":
            report["status"] = "completed"
        report["completed_epochs"] = len(report["history"])
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["elapsed_seconds"] = time.perf_counter() - started
        data.save_json(directory / "report.json", report)
    print(json.dumps({"status": report["status"], "report": str(directory / "report.json"),
                      "best_epoch": report["best_checkpoint"]["epoch"], "test_set_evaluated": False}, indent=2))
    return 0 if report["status"] == "completed" else 2


def predict(name, text, threshold):
    normalized = data.normalize(text)
    if not normalized:
        raise ValueError("Text must not be blank")
    configure_cpu()
    model, vectorizer, metadata = load_artifact(baseline.ARTIFACTS / run_name(name))
    vocabulary, idf = dict(vectorizer.vocabulary_), vectorizer.idf_.copy()
    x = features(vectorizer, [{"text": normalized}])
    with torch.no_grad():
        score = torch.sigmoid(model(x).squeeze(-1))
        finite(score, "prediction")
        score = float(score[0])
    assert_vectorizer_unchanged(vectorizer, vocabulary, idf)
    print(json.dumps({"text": text, "normalized_text": normalized, "spam_score": score, "threshold": threshold,
                      "label": "spam" if score >= threshold else "ham", "checkpoint_epoch": metadata["epoch"],
                      "run_name": name}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    training = commands.add_parser("train")
    training.add_argument("--run-name", type=run_name, default=DEFAULT_RUN)
    training.add_argument("--epochs", type=positive_integer, default=DEFAULTS["epochs"])
    training.add_argument("--hidden-units", type=positive_integer, default=DEFAULTS["hidden_units"])
    prediction = commands.add_parser("predict")
    prediction.add_argument("text")
    prediction.add_argument("--run-name", type=run_name, default=DEFAULT_RUN)
    prediction.add_argument("--threshold", type=baseline.threshold, default=0.5)
    args = parser.parse_args()
    if args.command == "train":
        return train(args.run_name, args.epochs, args.hidden_units)
    predict(args.run_name, args.text, args.threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
