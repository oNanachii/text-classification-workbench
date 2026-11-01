"""CPU inference and editable training-sample API; frozen data/model artifacts stay unchanged."""

from collections.abc import AsyncIterator, Callable
from typing import Any

from contextlib import asynccontextmanager
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
import torch

from legacy import baseline
from textlab import data
from legacy import train_mlp
from legacy.storage import SampleStore

MAX_TEXT_CHARACTERS = 2000
MAX_REVIEW_NOTE_CHARACTERS = 500
DEFAULT_THRESHOLD = 0.5
SHORT_HASH_LENGTH = 12


logger = logging.getLogger(__name__)
ModelId = Literal["lr", "mlp32", "mlp16"]
Threshold = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Label = Literal["ham", "spam"]
DATABASE_PATH = Path(os.environ.get("SMS_LAB_DB", data.DATA / "working.sqlite3"))
FRONTEND_DIST = data.ROOT / "frontend" / "dist"


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_TEXT_CHARACTERS)]
    model: ModelId
    threshold: Threshold = DEFAULT_THRESHOLD

    @field_validator("text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """Reject normalized blank text so prediction always has meaningful input."""
        if not data.normalize(value):
            raise ValueError("Text must not be blank")
        return value


class ModelInfo(BaseModel):
    id: ModelId
    name: str
    version: str


class PredictResponse(BaseModel):
    label: Literal["ham", "spam"]
    spam_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    threshold: Threshold
    model: ModelId
    model_version: str


class SampleResponse(BaseModel):
    id: str
    text: str
    original_label: Label
    label: Label
    revision: int
    updated_at: str


class SamplePage(BaseModel):
    items: list[SampleResponse]
    total: int
    limit: int
    offset: int


class LabelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Label
    note: Annotated[str, Field(strict=True, max_length=MAX_REVIEW_NOTE_CHARACTERS)] = ""


class CorrectionResult(BaseModel):
    sample: SampleResponse
    changed: bool
    correction_id: int | None


class CorrectionResponse(BaseModel):
    id: int
    sample_id: str
    old_label: Label
    new_label: Label
    note: str
    revision: int
    created_at: str


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load pinned local models and publish readiness only after storage is valid."""
    application.state.ready = False
    application.state.models = {}
    train_mlp.configure_cpu()
    bundle, prepared = baseline.load_model()
    models = {
        "lr": {
            "pipeline": bundle["pipeline"],
            "source_sha256": train_mlp.file_sha(baseline.MODEL_PATH),
            "provenance": {
                "split_sha256": bundle["split_sha256"],
                "raw_sha256": prepared["manifest"]["raw_sha256"],
                "normalization": prepared["manifest"]["normalization"],
                "validation_count": len(prepared["splits"]["validation"]),
                "validation_class_counts": prepared["manifest"]["counts"]["validation"],
            },
            "info": ModelInfo(
                id="lr",
                name="TF-IDF + Logistic Regression",
                version=(
                    f"baseline-v1-{train_mlp.file_sha(baseline.MODEL_PATH)[:SHORT_HASH_LENGTH]}"
                ),
            ),
        }
    }
    for model_id, run, width in (("mlp32", "mlp_cpu_v1", 32), ("mlp16", "mlp_cpu_h16_v1", 16)):
        model, vectorizer, metadata = train_mlp.load_artifact(baseline.ARTIFACTS / run)
        model.eval()
        model.requires_grad_(False)
        models[model_id] = {
            "network": model,
            "vectorizer": vectorizer,
            "metadata": metadata,
            "directory": baseline.ARTIFACTS / run,
            "info": ModelInfo(
                id=model_id,
                name=f"TF-IDF + MLP ({width} hidden units)",
                version=(
                    f"{run}-epoch{metadata['epoch']}-"
                    f"{metadata['model_file_sha256'][:SHORT_HASH_LENGTH]}"
                ),
            ),
        }
    # Only training rows enter an editable working copy; never change the frozen split file.
    store = SampleStore(DATABASE_PATH)
    store.initialize(prepared["splits"]["train"], bundle["split_sha256"])
    application.state.samples = store
    # Publish the cache only after both model artifacts and working storage are ready.
    application.state.models = models
    application.state.ready = True
    try:
        yield
    finally:
        application.state.ready = False
        application.state.models = {}
        application.state.samples = None


app = FastAPI(title="SMS Classification Teaching API", version="1.1.0", lifespan=lifespan)

if FRONTEND_DIST.is_dir():
    app.mount("/workbench", StaticFiles(directory=FRONTEND_DIST, html=True), name="workbench")

    @app.get("/", include_in_schema=False, response_model=None)
    def workbench_redirect() -> RedirectResponse:
        """Send the root page to the built workbench using its trailing-slash URL."""
        return RedirectResponse("/workbench/", status_code=307)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
    # Never echo inputs or validator contexts: these can contain NaN or exceptions.
    """Return validation details without echoing user text or unsafe validator context."""
    detail = [{key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


def require_ready(request: Request) -> None:
    """Reject requests until the complete local model cache is ready."""
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="Service is not ready")


def storage_call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Translate database failures into a stable API error without leaking internals."""
    try:
        return operation(*args, **kwargs)
    except sqlite3.DatabaseError:
        logger.exception("Sample storage operation failed")
        raise HTTPException(
            status_code=MAX_REVIEW_NOTE_CHARACTERS, detail="Storage operation failed"
        ) from None


@app.get("/health", response_model=None)
def health(request: Request) -> JSONResponse:
    """Report readiness so operators can distinguish startup from a working service."""
    ready = getattr(request.app.state, "ready", False)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"ready": ready, "status": "ready" if ready else "not_ready"},
    )


@app.get("/models", response_model=list[ModelInfo])
def list_models(request: Request) -> list[ModelInfo]:
    """List the models actually loaded to make available choices explicit."""
    require_ready(request)
    return [entry["info"] for entry in request.app.state.models.values()]


def experiment_summary(models: dict[str, Any]) -> dict[str, Any]:
    """Read existing reports only; bind their evidence to the models serving requests."""
    provenance = models["lr"]["provenance"]
    reports = {}
    for model_id in ("mlp32", "mlp16"):
        entry = models[model_id]
        report = json.loads((entry["directory"] / "report.json").read_text(encoding="utf-8"))
        comparison = report["comparison_at_threshold_0_5"]
        if (
            report["status"] != "completed"
            or report["evaluation_split"] != "validation"
            or report["test_set_evaluated"] is not False
            or comparison["split"] != "validation"
            or comparison["threshold"] != DEFAULT_THRESHOLD
            or comparison["same_validation_rows"] is not True
            or report["best_checkpoint"] != entry["metadata"]
            or report["config"] != entry["metadata"]["config"]
            or report["versions"] != entry["metadata"]["versions"]
            or comparison["mlp"]["epoch"] != entry["metadata"]["epoch"]
            or any(
                entry["metadata"][key] != provenance[key]
                for key in ("split_sha256", "raw_sha256", "normalization")
            )
            or any(report["data"][key] != value for key, value in provenance.items())
            or report["data"]["source_baseline_file_sha256"] != models["lr"]["source_sha256"]
            or not report["data"]["near_duplicate_check"].startswith("not performed")
        ):
            raise ValueError("Report provenance does not match loaded artifacts")
        reports[model_id] = report
    if (
        reports["mlp32"]["comparison_at_threshold_0_5"]["logistic_regression"]
        != reports["mlp16"]["comparison_at_threshold_0_5"]["logistic_regression"]
    ):
        raise ValueError("Baseline metrics disagree between reports")

    sample_count, counts = provenance["validation_count"], provenance["validation_class_counts"]
    if set(counts) != {"ham", "spam"} or sum(counts.values()) != sample_count:
        raise ValueError("Invalid validation sample counts")
    result = []
    for model_id, name in (
        ("lr", "逻辑回归"),
        ("mlp32", "32维神经网络"),
        ("mlp16", "16维神经网络"),
    ):
        entry = models[model_id]
        report = reports["mlp32" if model_id == "lr" else model_id]
        source = report["comparison_at_threshold_0_5"][
            "logistic_regression" if model_id == "lr" else "mlp"
        ]
        metrics = {
            target: source[original]
            for target, original in (
                ("accuracy", "accuracy"),
                ("precision", "spam_precision"),
                ("recall", "spam_recall"),
                ("macro_f1", "macro_f1"),
                *(
                    (key, key)
                    for key in (
                        "true_positive",
                        "false_positive",
                        "false_negative",
                        "true_negative",
                    )
                ),
            )
        }
        if (
            any(
                not math.isfinite(metrics[key]) or not 0 <= metrics[key] <= 1
                for key in ("accuracy", "precision", "recall", "macro_f1")
            )
            or any(
                type(metrics[key]) is not int or metrics[key] < 0
                for key in ("true_positive", "false_positive", "false_negative", "true_negative")
            )
            or metrics["true_positive"] + metrics["false_negative"] != counts["spam"]
            or metrics["true_negative"] + metrics["false_positive"] != counts["ham"]
        ):
            raise ValueError("Invalid report metrics")
        history = (
            []
            if model_id == "lr"
            else [
                {key: row[key] for key in ("epoch", "train_loss", "validation_loss")}
                for row in report["history"]
            ]
        )
        if model_id != "lr" and (
            not history
            or any(
                type(row["epoch"]) is not int
                or row["epoch"] < 1
                or any(
                    not math.isfinite(row[key]) or row[key] < 0
                    for key in ("train_loss", "validation_loss")
                )
                for row in history
            )
        ):
            raise ValueError("Invalid training history")
        classifier = entry.get("pipeline")
        parameter_count = (
            int(classifier["model"].coef_.size + classifier["model"].intercept_.size)
            if classifier is not None
            else sum(p.numel() for p in entry["network"].parameters())
        )
        result.append(
            {
                "id": model_id,
                "name": name,
                "parameter_count": parameter_count,
                "checkpoint_epoch": None if model_id == "lr" else entry["metadata"]["epoch"],
                "metrics": metrics,
                "history": history,
            }
        )
    return {
        "evaluation_split": "validation",
        "sample_count": sample_count,
        "class_counts": counts,
        "threshold": DEFAULT_THRESHOLD,
        "test_set_evaluated": False,
        "near_duplicate_checked": False,
        "models": result,
        "notes": [
            "仅英语短信教学数据，尚未做中文或线上流量验收。",
            "近重复、改写和共享模板未检查，可能跨集合出现。",
            "验证集用于模型和检查点选择；测试集未评估，不能据此宣称最终泛化效果。",
            "工作区标签修订不改变冻结数据和既有模型；这些模型尚未据此重新训练。",
            "固定随机种子不保证跨设备或依赖版本完全一致。",
        ],
    }


@app.get("/experiments", response_model=None)
def list_experiments(request: Request) -> Any:
    """Read verified existing validation reports without running a new evaluation."""
    require_ready(request)
    try:
        return experiment_summary(request.app.state.models)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.exception("Existing experiment report is unavailable or inconsistent")
        raise HTTPException(
            status_code=503, detail="Experiment reports are unavailable or inconsistent"
        ) from None


@app.post("/predict", response_model=PredictResponse)
def predict(payload: PredictRequest, request: Request) -> PredictResponse:
    """Predict with the loaded numeric artifacts without fitting or updating weights."""
    require_ready(request)
    entry = request.app.state.models[payload.model]
    try:
        normalized = data.normalize(payload.text)
        if payload.model == "lr":
            score = float(baseline.spam_scores(entry["pipeline"], [normalized])[0])
        else:
            # features() only transforms with the saved vocabulary and IDF.
            features = train_mlp.features(entry["vectorizer"], [{"text": normalized}])
            with torch.no_grad():
                score = float(torch.sigmoid(entry["network"](features)).item())
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise FloatingPointError("Invalid model score")
        return PredictResponse(
            label="spam" if score >= payload.threshold else "ham",
            spam_score=score,
            threshold=payload.threshold,
            model=payload.model,
            model_version=entry["info"].version,
        )
    except Exception:
        # No request body or SMS text is logged; retain traceback for diagnosis.
        logger.exception("Inference failed for model %s", payload.model)
        raise HTTPException(
            status_code=MAX_REVIEW_NOTE_CHARACTERS, detail="Prediction failed"
        ) from None


@app.get("/samples", response_model=SamplePage)
def list_samples(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    label: Label | None = None,
    q: Annotated[str, Query(max_length=200)] = "",
) -> Any:
    """Page editable training samples through the validated storage boundary."""
    require_ready(request)
    return storage_call(
        request.app.state.samples.list_samples,
        limit=limit,
        offset=offset,
        label=label,
        query=data.normalize(q),
    )


@app.get("/samples/{sample_id}", response_model=SampleResponse)
def get_sample(sample_id: str, request: Request) -> Any:
    """Read a training sample and report absence without exposing storage details."""
    require_ready(request)
    sample = storage_call(request.app.state.samples.get_sample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample


@app.patch("/samples/{sample_id}/label", response_model=CorrectionResult)
def update_sample_label(sample_id: str, payload: LabelUpdate, request: Request) -> Any:
    """Commit a training-label correction and its audit history together."""
    require_ready(request)
    result = storage_call(
        request.app.state.samples.correct_label, sample_id, payload.label, payload.note
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return result


@app.get("/samples/{sample_id}/corrections", response_model=list[CorrectionResponse])
def sample_corrections(sample_id: str, request: Request) -> Any:
    """Read the audit trail only after confirming the working sample exists."""
    require_ready(request)
    if storage_call(request.app.state.samples.get_sample, sample_id) is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return storage_call(request.app.state.samples.correction_history, sample_id)


# Route docs are for source readers. Preserve the original public OpenAPI contract
# while keeping annotations from implicitly enabling response-model serialization.
for route in app.routes:
    if isinstance(route, APIRoute):
        route.description = ""
