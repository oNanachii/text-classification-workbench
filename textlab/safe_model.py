"""Small inference-only numeric format: JSON vocabulary plus NPZ, never pickle or Torch."""

from __future__ import annotations

import math
from pathlib import Path
import re
import numpy as np
from textlab.dataflow import sha, read_json
from textlab import data

MAX_FEATURES = 2000
MAX_TEXT_CHARS = 2000
MLP_HIDDEN_UNITS = 16
LOGIT_CLIP = 80

TOKEN = re.compile(r"(?u)\b\w\w+\b")


class SafeModel:
    """Run the trained LR or MLP from checksum-verified JSON and NPZ artifacts."""

    def __init__(self, directory: str | Path, kind: str) -> None:
        """Load checksum-verified numeric artifacts to avoid executable model formats."""
        directory = Path(directory)
        self.metadata = read_json(directory / "model.json")
        if kind not in {"lr", "mlp16"}:
            raise ValueError("Unknown model kind")
        self.kind = kind
        weights = directory / "weights.npz"
        vocab = directory / "vocabulary.json"
        for path, key in ((weights, "weights_sha256"), (vocab, "vocabulary_sha256")):
            if path.is_symlink() or sha(path.read_bytes()) != self.metadata[key]:
                raise ValueError("Numeric artifact checksum mismatch")
        vector = read_json(vocab)
        self.vocabulary = vector["vocabulary"]
        self.idf = np.asarray(vector["idf"], dtype=np.float64)
        n = len(self.vocabulary)
        if (
            n < 1
            or n > MAX_FEATURES
            or sorted(self.vocabulary.values()) != list(range(n))
            or self.idf.shape != (n,)
            or not np.isfinite(self.idf).all()
        ):
            raise ValueError("Invalid vocabulary")
        with np.load(weights, allow_pickle=False) as values:
            self.weights = {k: values[k].copy() for k in values.files}
        expected = {
            "lr_coef": (n,),
            "lr_intercept": (1,),
            "mlp_w1": (MLP_HIDDEN_UNITS, n),
            "mlp_b1": (MLP_HIDDEN_UNITS,),
            "mlp_w2": (1, MLP_HIDDEN_UNITS),
            "mlp_b2": (1,),
        }
        if set(self.weights) != set(expected) or any(
            self.weights[k].shape != shape or not np.isfinite(self.weights[k]).all()
            for k, shape in expected.items()
        ):
            raise ValueError("Invalid numeric weights")

    def features(self, texts: list[str]) -> np.ndarray:
        """Reproduce saved TF-IDF features so inference matches the trained vectorizer."""
        x = np.zeros((len(texts), len(self.vocabulary)), dtype=np.float64)
        for i, text in enumerate(texts):
            for token in TOKEN.findall(data.normalize(text)):
                index = self.vocabulary.get(token)
                if index is not None:
                    x[i, index] += 1
        x *= self.idf
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        np.divide(x, norms, out=x, where=norms > 0)
        return x

    def scores(self, texts: list[str]) -> np.ndarray:
        """Compute unchanged logistic or MLP scores using the saved numeric weights."""
        x = self.features(texts)
        w = self.weights
        if self.kind == "lr":
            logits = x @ w["lr_coef"] + w["lr_intercept"][0]
        else:
            hidden = np.maximum(0, x.astype(np.float32) @ w["mlp_w1"].T + w["mlp_b1"])
            logits = (hidden @ w["mlp_w2"].T + w["mlp_b2"]).ravel()
        return 1 / (1 + np.exp(-np.clip(logits, -LOGIT_CLIP, LOGIT_CLIP)))

    def predict(self, text: str, threshold: float) -> dict[str, str | float]:
        """Validate one English SMS and apply the selected threshold for serving."""
        if (
            not isinstance(text, str)
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
        ):
            raise ValueError("Invalid prediction input")
        normalized = data.normalize(text)
        if (
            not normalized
            or len(text) > MAX_TEXT_CHARS
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("Invalid prediction input")
        if not re.search(r"[a-zA-Z]{2}", normalized):
            raise ValueError("First version requires English SMS text")
        if not any(token in self.vocabulary for token in TOKEN.findall(normalized)):
            raise ValueError("Text has no recognized vocabulary; prediction was not made")
        score = float(self.scores([normalized])[0])
        return {
            "label": "spam" if score >= threshold else "ham",
            "spam_score": score,
            "threshold": threshold,
        }
