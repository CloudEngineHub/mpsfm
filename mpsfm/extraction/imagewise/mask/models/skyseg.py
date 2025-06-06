import copy
from pathlib import Path

import cv2
import numpy as np
import onnxruntime

from mpsfm.extraction.base_model import BaseModel

# https://github.com/google/sky-optimization/commit/8a35938afe8dc0da931d960bfe05d0c99a9b40e3


def bias(x, b=0.8):
    denom = ((1 / b) - 2) * (1 - x) + 1
    return x / denom


def probability_to_confidence(probabilty, low_thresh=0.3, high_thresh=0.5):
    eps = 0.01

    low = probabilty < low_thresh
    high = probabilty > high_thresh
    confidence_low = bias((low_thresh - probabilty[low]) / low_thresh)
    confidence_high = bias((probabilty[high] - high_thresh) / (1 - high_thresh))
    confidence = np.zeros_like(probabilty)
    confidence[low] = confidence_low
    confidence[high] = confidence_high
    confidence = np.maximum(eps, confidence)
    return confidence


def downsample2_antialiased(X):
    kernel = np.array([1, 3, 3, 1]) / 8
    dst = cv2.sepFilter2D(X, -1, kernel, kernel, anchor=(1, 1), borderType=cv2.BORDER_REPLICATE)
    return dst[::2, ::2]


def resize_helper(X, shape):
    X = X.squeeze()
    while np.all(np.array(X.shape[:2]) >= np.array(shape) * 2):
        X = downsample2_antialiased(X)
    return cv2.resize(X, dsize=tuple(shape[1::-1]), interpolation=cv2.INTER_LINEAR)


def resize(X, shape):
    if X.ndim == 2 or X.shape[2] <= 4:
        return resize_helper(X, shape)
    # opencv doesn't work on more than 4 channels
    X1 = resize_helper(X[..., :3], shape)
    X2 = resize_helper(X[..., 3:], shape)
    return np.concatenate([X1, X2], axis=2)


def outer_product_images(X, Y):
    assert X.shape[-1] == 3 and Y.shape[-1] == 3
    X_flat = X[..., :, None]
    Y_flat = Y[..., None, :]

    outer = np.matmul(X_flat, Y_flat)
    ind = np.triu_indices(3)
    outer = outer[..., ind[0], ind[1]]
    return outer.reshape(X.shape[:-1] + (6,))


def smooth_upsample(X, size, num_steps=None):
    if num_steps is None:
        log4ratio = np.max(0.5 * np.log2(np.array(size) / X.shape[:2]))
        num_steps = np.maximum(1, log4ratio.round().astype(int))
    ratio = np.array(size) / X.shape[:2]
    ratio_per_step = np.array(X.shape[:2]) * ratio / num_steps
    for step in np.arange(1, num_steps + 1):
        target_shape_for_step = np.round(step * ratio_per_step).astype(int)
        X = resize(X, target_shape_for_step)
    return X


def solve_image_ldl3(A, b):
    A11, A12, A13, A22, A23, A33 = np.split(A, A.shape[-1], axis=-1)
    b1, b2, b3 = np.split(b, b.shape[-1], axis=-1)
    d1 = A11
    L_12 = A12 / d1
    d2 = A22 - L_12 * A12
    L_13 = A13 / d1
    L_23 = (A23 - L_13 * A12) / d2
    d3 = A33 - L_13 * A13 - L_23 * L_23 * d2
    y1 = b1
    y2 = b2 - L_12 * y1
    y3 = b3 - L_13 * y1 - L_23 * y2
    x3 = y3 / d3
    x2 = y2 / d2 - L_23 * x3
    x1 = y1 / d1 - L_12 * x2 - L_13 * x3
    return np.stack([x1, x2, x3], axis=-1).squeeze()


def weighted_downsample(X, confidence, scale=None, target_size=None):
    if target_size is None:
        target_size = (np.array(X.shape[:2]) / scale).round().astype(int)
    if X.shape[1] > confidence.shape[1]:
        X = resize(X, confidence.shape)
    if X.ndim == 3:
        confidence = confidence[..., None]
    numerator = resize(X * confidence, target_size)
    denom = resize(confidence, target_size)
    if X.ndim == 3:
        denom = denom[..., None]
    return numerator / denom


def guided_upsample(
    reference,
    source,
    kernel_size,
    confidence=None,
    eps_luma=1e-2,
    eps_chroma=1e-2,
    clip_output=True,
):
    assert reference.shape[2] == 3

    if np.any(np.array(source.shape) < np.array(reference.shape[:2])):
        source = resize(source, reference.shape[:2])
    if confidence is None:
        confidence = probability_to_confidence(source)
    assert confidence.shape == source.shape

    reference_small = weighted_downsample(reference, confidence, kernel_size)
    small_shape = reference_small.shape[:2]
    source_small = weighted_downsample(source, confidence, target_size=small_shape)

    outer_reference = outer_product_images(reference, reference)
    outer_reference = weighted_downsample(outer_reference, confidence, target_size=small_shape)
    covar = outer_reference - outer_product_images(reference_small, reference_small)
    var = weighted_downsample(reference * source[..., None], confidence, target_size=small_shape)
    residual_small = var - reference_small * source_small[..., None]
    covar[..., 0] += eps_luma**2
    covar[..., [3, 5]] += eps_chroma**2

    affine = solve_image_ldl3(covar, residual_small)
    residual = source_small - (affine * reference_small).sum(axis=2)
    affine = smooth_upsample(affine, reference.shape[:2])
    residual = smooth_upsample(residual, reference.shape[:2])
    output = (affine * reference).sum(axis=2) + residual
    if clip_output:
        output = output.clip(0, 1)
    return output


def run_inference(onnx_session, input_size, image):
    """copied from onnx_interence.py"""
    # Pre process:Resize, BGR->RGB, Transpose, PyTorch standardization, float32 cast
    temp_image = copy.deepcopy(image)
    resize_image = cv2.resize(temp_image, dsize=(input_size[0], input_size[1]))
    x = cv2.cvtColor(resize_image, cv2.COLOR_BGR2RGB)
    x = np.array(x, dtype=np.float32)
    mean = [0.485, 0.456, 0.406]
    std = [0.229, 0.224, 0.225]
    x = (x / 255 - mean) / std
    x = x.transpose(2, 0, 1)
    x = x.reshape(-1, 3, input_size[0], input_size[1]).astype("float32")

    # Inference
    input_name = onnx_session.get_inputs()[0].name
    output_name = onnx_session.get_outputs()[0].name
    onnx_result = onnx_session.run([output_name], {input_name: x})

    # Post process
    onnx_result = np.array(onnx_result).squeeze()
    min_value = np.min(onnx_result)
    max_value = np.max(onnx_result)
    onnx_result = (onnx_result - min_value) / (max_value - min_value)
    onnx_result *= 255
    onnx_result = onnx_result.astype("uint8")

    return onnx_result


class Skyseg(BaseModel):
    default_conf = {
        "return_types": ["mask"],
        "scale": 1,
        "thresh": 0.5,
        "model_name": "skyseg.onnx",
        "require_download": True,
        "download_url": "1jJpcRXAHaTR1zk4xD1kVYXtnO1-C982K",
        "download_method": "gdown",
    }
    name = "skyseg"

    def _init(self, conf):
        self.onnx_session = onnxruntime.InferenceSession(Path(self.conf.models_dir, self.conf.model_name).as_posix())

    def _forward(self, data):
        image = data["image"]
        original_image = image.copy()
        image = image[..., ::-1]
        while image.shape[0] >= 640 and image.shape[1] >= 640:
            image = cv2.pyrDown(image)

        mask = run_inference(self.onnx_session, [320, 320], image) / 255
        kernel_size = 64 * 4
        original_image = original_image.astype(np.float64) / 255
        mask = guided_upsample(original_image, mask, kernel_size) <= self.conf.thresh

        out_kwargs = {
            key: val
            for key, val in dict(
                mask=mask,
            ).items()
        }

        out_kwargs = {k: v.squeeze() for k, v in out_kwargs.items() if k in self.conf.return_types}
        return out_kwargs
