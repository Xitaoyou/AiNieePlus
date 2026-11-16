"""Native CPU detection and recognition; no ImageTrans or Java process."""

from functools import lru_cache
import re
import numpy as np
from PIL import Image
from Backend.application.pipeline_common import PipelineError


@lru_cache(maxsize=3)
def session(path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    return ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def detect(image, folder, model_id, confidence=0.5):
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    if model_id == "legacy-balloon-yolov4":
        import json

        params = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
        net = cv2.dnn.readNetFromDarknet(
            str(folder / "model.cfg"), str(folder / "model.weights")
        )
        detector = cv2.dnn_DetectionModel(net)
        detector.setInputParams(
            size=(params.get("width", 512), params.get("height", 512)),
            scale=1 / 255,
            swapRB=params.get("swapRB", True),
            crop=params.get("crop", False),
        )
        classes, scores, boxes = detector.detect(
            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            confThreshold=confidence,
            nmsThreshold=0.4,
        )
        return [
            {
                "rect": [float(x), float(y), float(w), float(h)],
                "score": float(s),
                "class_id": 1,
            }
            for (x, y, w, h), s in zip(boxes, scores)
        ]
    if model_id != "manga-panel":
        raise PipelineError("MODEL_UNSUPPORTED", "该模型尚未适配检测引擎", 422)
    engine = session(str(folder / "model.onnx"))
    # Match ImageTrans YOLO26: 640x640 letterbox, RGB float CHW, end-to-end detections.
    scale = min(640 / width, 640 / height)
    w, h = round(width * scale), round(height * scale)
    left, top = (640 - w) // 2, (640 - h) // 2
    canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
    canvas[top : top + h, left : left + w] = cv2.resize(rgb, (w, h))
    tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255
    output = np.asarray(
        engine.run(None, {engine.get_inputs()[0].name: tensor})[0]
    ).squeeze(0)
    if output.ndim != 2 or output.shape[-1] != 6:
        raise PipelineError(
            "MODEL_OUTPUT_MISMATCH", "YOLO26 模型输出不是 [N,6]，请检查权重版本", 422
        )
    result = []
    for x0, y0, x1, y1, score, cls in output:
        if (
            not np.isfinite([x0, y0, x1, y1, score, cls]).all()
            or score < confidence
            or int(cls) not in (0, 1)
        ):
            continue
        x0, x1 = np.clip([(x0 - left) / scale, (x1 - left) / scale], 0, width)
        y0, y1 = np.clip([(y0 - top) / scale, (y1 - top) / scale], 0, height)
        if x1 > x0 and y1 > y0:
            result.append(
                {
                    "rect": [float(x0), float(y0), float(x1 - x0), float(y1 - y0)],
                    "score": float(score),
                    "class_id": int(cls),
                }
            )
    return result


def recognize(image, folder, engine_id, cancelled=lambda: False):
    if engine_id == "rapidocr":
        from rapidocr import RapidOCR

        engine = RapidOCR(
            params={
                "Det.model_path": str(folder / "det.onnx"),
                "Rec.model_path": str(folder / "rec.onnx"),
                "Cls.model_path": str(folder / "cls.onnx"),
            }
        )
        result = engine(np.asarray(image.convert("RGB")))
        return "\n".join(result.txts or [])
    if engine_id != "mangaocr":
        raise PipelineError("MODEL_UNSUPPORTED", "未知 OCR 引擎", 422)
    import jaconv

    encoder, decoder = (
        session(str(folder / "encoder.onnx")),
        session(str(folder / "decoder.onnx")),
    )
    pixels = np.asarray(
        image.convert("L").convert("RGB").resize((224, 224), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )
    pixels = ((pixels / 255 - 0.5) / 0.5).transpose(2, 0, 1)[None]
    hidden = encoder.run(["encoder_hidden_states"], {"pixel_values": pixels})[0]
    tokens = [2]
    for _ in range(300):
        if cancelled():
            raise PipelineError("CANCELLED", "OCR 已取消", 409)
        logits = decoder.run(
            ["logits"],
            {
                "input_ids": np.array([tokens], dtype=np.int64),
                "encoder_hidden_states": hidden,
            },
        )[0]
        token = int(logits[0, -1].argmax())
        tokens.append(token)
        if token == 3:
            break
    vocab = (folder / "vocab.txt").read_text(encoding="utf-8").splitlines()
    text = "".join(vocab[t] for t in tokens if 5 <= t < len(vocab))
    text = "".join(text.split()).replace("…", "...")
    text = re.sub(r"[・.]{2,}", lambda m: "." * len(m[0]), text)
    return jaconv.h2z(text, ascii=True, digit=True)


def worker(
    operation, image_path, folder, model_id, confidence=0.5, rect=None, render_data=None
):
    """Picklable process boundary: model crashes cannot terminate the API/Qt process."""
    from pathlib import Path

    if operation == "render":
        from Backend.infrastructure.manga.render import materialize
        from Backend.infrastructure.manga.models import ModelRegistry
        from Backend.domain.models.manga import MangaPage

        page = MangaPage.model_validate(render_data["page"])
        if not render_data.get("erase_untranslated"):
            for region in page.regions:
                if not region.target.strip():
                    region.removal = "none"
        return materialize(
            page,
            Path(image_path),
            Path(render_data["directory"]),
            render_data["revision"],
            ModelRegistry(root=folder or None, bundled=render_data["bundled"] or None),
        )
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        if operation == "detect":
            return detect(image, Path(folder), model_id, confidence)
        if operation == "ocr":
            if rect:
                x, y, w, h = rect
                image = image.crop((int(x), int(y), int(x + w), int(y + h)))
            return recognize(image, Path(folder), model_id)
    raise ValueError("未知模型操作")
