"""Conversión a ONNX (ADR-05) y evaluación con ONNX Runtime.

Contrato de los modelos exportados: entrada float32 `[None, n_features]` llamada INPUT_NAME;
salida: probabilidad de la clase positiva (tras `run`, ver `predict_positive_proba`).
"""

from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnxmltools import convert_xgboost
from onnxmltools.convert.common.data_types import FloatTensorType as MLToolsFloatTensorType
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
from sklearn.pipeline import Pipeline
from xgboost import XGBClassifier

INPUT_NAME = "features"
# skl2onnx emite (label, probabilities); la clase positiva es la columna 1.
POSITIVE_CLASS_COLUMN = 1
TARGET_OPSET = 15


def sklearn_pipeline_to_onnx(pipeline: Pipeline, n_features: int) -> onnx.ModelProto:
    """Pipeline (StandardScaler + LogisticRegression) a ONNX, con zipmap desactivado."""
    return convert_sklearn(
        pipeline,
        initial_types=[(INPUT_NAME, FloatTensorType([None, n_features]))],
        options={id(pipeline.steps[-1][1]): {"zipmap": False}},
        target_opset=TARGET_OPSET,
    )


def xgboost_to_onnx(model: XGBClassifier, n_features: int) -> onnx.ModelProto:
    """XGBClassifier a ONNX con onnxmltools."""
    return convert_xgboost(
        model,
        initial_types=[(INPUT_NAME, MLToolsFloatTensorType([None, n_features]))],
        target_opset=TARGET_OPSET,
    )


def save_onnx(model: onnx.ModelProto, path: Path) -> None:
    path.write_bytes(model.SerializeToString())


def load_session(path: Path | bytes) -> ort.InferenceSession:
    """Sesión de ONNX Runtime en CPU y con un solo hilo."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(
        str(path) if isinstance(path, Path) else path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


def predict_positive_proba(path: Path | bytes, X: np.ndarray) -> np.ndarray:
    """Carga un .onnx y devuelve la probabilidad de la clase positiva, float64 de forma (n,)."""
    session = load_session(path)
    outputs = session.run(None, {INPUT_NAME: np.asarray(X, dtype=np.float32)})
    probabilities = outputs[1]
    return np.asarray(probabilities, dtype=np.float64)[:, POSITIVE_CLASS_COLUMN]
