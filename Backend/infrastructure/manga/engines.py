"""Native CPU detection and recognition; no ImageTrans or Java process."""

import re
from functools import lru_cache

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


def _model_backend(folder, model_id):
    """Spec 后端（优先）或 model.json 自带的 rapidocr 标记。"""
    from Backend.infrastructure.manga.models import CATALOGUE

    spec = next((s for s in CATALOGUE if s["id"] == model_id), {})
    backend = spec.get("backend", "")
    if not backend:
        try:
            import json

            params = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
            backend = "rapidocr_det" if params.get("rapidocr") else "yolo26"
        except (OSError, ValueError):
            backend = "yolo26"
    return backend


def _ppocr_det(image, folder, confidence):
    """PP-OCR det 分割模型：概率图 → 阈值 → 最小外接矩形。"""
    import cv2
    import json

    params = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
    size_w = int(params.get("width", 640))
    size_h = int(params.get("height", 640))
    box_thresh = float(params.get("box_thresh", 0.5) or 0.5)
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(size_w / width, size_h / height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    canvas = np.zeros((size_h, size_w, 3), dtype=np.uint8)
    canvas[:h, :w] = cv2.resize(rgb, (w, h))
    tensor = (
        canvas.astype(np.float32) / 255.0
    ).transpose(2, 0, 1)[None]
    engine = session(str(folder / "model.onnx"))
    output = np.asarray(
        engine.run(None, {engine.get_inputs()[0].name: tensor})[0]
    )
    if output.ndim == 4:
        prob = output[0, 0]
    elif output.ndim == 3:
        prob = output[0]
    else:
        prob = output
    mask = (prob > box_thresh).astype(np.uint8) * 255
    mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    boxes = []
    for contour in contours:
        if cv2.contourArea(contour) < 12:
            continue
        (cx, cy), (bw, bh), angle = cv2.minAreaRect(contour)
        if bw < 4 or bh < 4:
            continue
        x0 = max(0.0, min(cx - bw / 2, cx - bh / 2) / scale)
        y0 = max(0.0, min(cy - bw / 2, cy - bh / 2) / scale)
        x1 = min(width, max(cx + bw / 2, cx + bh / 2) / scale)
        y1 = min(height, max(cy + bw / 2, cy + bh / 2) / scale)
        if x1 > x0 and y1 > y0:
            boxes.append(
                {
                    "rect": [float(x0), float(y0), float(x1 - x0), float(y1 - y0)],
                    "score": float(min(1.0, box_thresh + 0.1)),
                    "class_id": 1,
                }
            )
    return boxes


def _yolo_v8(image, folder, confidence):
    """YOLOv8 经典输出 [1, 4+nc, anchors]：转置 + 置信度过滤 + NMS。"""
    import cv2
    import json

    params = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
    size_w = int(params.get("width", 640) or 640)
    size_h = int(params.get("height", 640) or 640)
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(size_w / width, size_h / height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    left, top = (size_w - w) // 2, (size_h - h) // 2
    canvas = np.full((size_h, size_w, 3), 114, dtype=np.uint8)
    canvas[top : top + h, left : left + w] = cv2.resize(rgb, (w, h))
    tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255
    engine = session(str(folder / "model.onnx"))
    output = np.asarray(engine.run(None, {engine.get_inputs()[0].name: tensor})[0])
    arr = output.squeeze(0)
    if arr.ndim != 2:
        raise PipelineError(
            "MODEL_OUTPUT_MISMATCH", "YOLOv8 输出维度异常", 422
        )
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T  # [anchors, 4+nc]
    boxes, scores, class_ids = [], [], []
    for row_values in arr:
        if len(row_values) < 5:
            continue
        xywh = row_values[:4]
        class_scores = row_values[4:]
        if not np.isfinite(xywh).all() or not np.isfinite(class_scores).all():
            continue
        if class_scores.size == 0:
            continue
        class_id = int(class_scores.argmax())
        score = float(class_scores[class_id])
        if score < confidence:
            continue
        cx, cy, bw, bh = (float(v) for v in xywh)
        x0 = max(0.0, (cx - bw / 2 - left) / scale)
        y0 = max(0.0, (cy - bh / 2 - top) / scale)
        x1 = min(float(width), (cx + bw / 2 - left) / scale)
        y1 = min(float(height), (cy + bh / 2 - top) / scale)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        boxes.append([x0, y0, x1 - x0, y1 - y0])
        scores.append(score)
        class_ids.append(class_id)
    if not boxes:
        return []
    indices = cv2.dnn.NMSBoxes(boxes, scores, confidence, 0.45)
    result = []
    for index in np.asarray(indices).flatten():
        rect = boxes[int(index)]
        # 这些模型按“气泡/文字”训练，统一作为文字框处理，避免误当分镜
        result.append(
            {
                "rect": [float(v) for v in rect],
                "score": float(scores[int(index)]),
                "class_id": 1,
            }
        )
    return result


def detect(image, folder, model_id, confidence=0.5):
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    backend = _model_backend(folder, model_id)
    if backend == "rapidocr_det":
        return _ppocr_det(image, folder, confidence)
    if backend == "yolo_v8":
        return _yolo_v8(image, folder, confidence)
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
    if backend in {"layout", "rfdetr"}:
        raise PipelineError(
            "MODEL_UNSUPPORTED",
            "该版面/检测模型的推理尚未适配，请先使用 manga-panel 或 japanese-lines",
            422,
        )
    if backend not in {"yolo26", "yolo_generic"}:
        raise PipelineError("MODEL_UNSUPPORTED", "该模型尚未适配检测引擎", 422)
    engine = session(str(folder / "model.onnx"))
    size_w = size_h = 640
    try:
        import json

        params = json.loads((folder / "model.json").read_text(encoding="utf-8-sig"))
        size_w = int(params.get("width", 640) or 640)
        size_h = int(params.get("height", 640) or 640)
    except (OSError, ValueError):
        pass
    # Match ImageTrans YOLO: letterbox, RGB float CHW, end-to-end detections.
    scale = min(size_w / width, size_h / height)
    w, h = round(width * scale), round(height * scale)
    left, top = (size_w - w) // 2, (size_h - h) // 2
    canvas = np.full((size_h, size_w, 3), 114, dtype=np.uint8)
    canvas[top : top + h, left : left + w] = cv2.resize(rgb, (w, h))
    tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255
    output = np.asarray(
        engine.run(None, {engine.get_inputs()[0].name: tensor})[0]
    ).squeeze(0)
    if output.ndim != 2 or output.shape[-1] not in (5, 6):
        raise PipelineError(
            "MODEL_OUTPUT_MISMATCH",
            "模型输出不是 [N,5] 或 [N,6]，该权重可能属于其他架构",
            422,
        )
    has_class = output.shape[-1] == 6
    result = []
    for row_values in output:
        if has_class:
            x0, y0, x1, y1, score, cls = row_values
        else:
            x0, y0, x1, y1, score = row_values
            cls = 1.0
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


_ENGINE_CACHE = {}


def _cached_engine(key, factory):
    engine = _ENGINE_CACHE.get(key)
    if engine is None:
        engine = factory()
        _ENGINE_CACHE[key] = engine
    return engine


def _ocr_openai(image, config):
    import base64
    from io import BytesIO

    import requests

    base = (config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = config.get("model") or "gpt-4o-mini"
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    response = requests.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + (config.get("api_key") or "")},
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "只输出图片中的文字，不要解释。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + data},
                        },
                    ],
                }
            ],
            "temperature": 0,
        },
        timeout=(15, 120),
    )
    response.raise_for_status()
    payload = response.json()
    return (payload["choices"][0]["message"]["content"] or "").strip()


def _ocr_gemini(image, config):
    import base64
    from io import BytesIO

    import requests

    model = config.get("model") or "gemini-2.0-flash"
    base = (
        config.get("base_url")
        or "https://generativelanguage.googleapis.com/v1beta"
    ).rstrip("/")
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    response = requests.post(
        f"{base}/models/{model}:generateContent",
        params={"key": config.get("api_key") or ""},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": "只输出图片中的文字，不要解释。"},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": data,
                            }
                        },
                    ]
                }
            ]
        },
        timeout=(15, 120),
    )
    response.raise_for_status()
    candidates = response.json().get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def _ocr_custom(image, config):
    """用户自建 OCR 网关：POST base_url {image: base64} → data.text / text。"""
    import base64
    from io import BytesIO

    import requests

    url = config.get("base_url") or ""
    if not url:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "该服务商需要专用协议：请在配置里填入兼容网关地址（POST base64 图片）",
            409,
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    response = requests.post(
        url,
        headers=(
            {"Authorization": "Bearer " + config["api_key"]}
            if config.get("api_key")
            else {}
        ),
        json={"image": data, "model": config.get("model") or "default"},
        timeout=(15, 120),
    )
    response.raise_for_status()
    payload = response.json()
    for key in ("text", "result", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    data_field = payload.get("data") or {}
    if isinstance(data_field, dict):
        for key in ("text", "result", "content"):
            if isinstance(data_field.get(key), str):
                return data_field[key].strip()
    raise PipelineError(
        "MODEL_API_RESPONSE", "网关返回里没有找到文字字段（text/result/content）", 409
    )


def _ocr_winrt(image):
    try:
        import asyncio

        import winsdk.windows.media.ocr as ocr
        import winsdk.windows.storage.streams as streams
        from winsdk.windows.graphics.imaging import SoftwareBitmap
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.security.cryptography import CryptographicBuffer
    except ImportError as error:
        raise PipelineError(
            "MODEL_DEPENDENCY_MISSING",
            "Windows 系统 OCR 需要 winsdk 包：pip install winsdk",
            409,
        ) from error
    from io import BytesIO

    buffer = BytesIO()
    image.convert("RGB").save(buffer, "BMP")
    data = buffer.getvalue()

    async def run():
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise PipelineError(
                "MODEL_DEPENDENCY_MISSING",
                "系统未安装 OCR 语言包，请在 Windows 设置中安装",
                409,
            )
        stream = streams.InMemoryRandomAccessStream()
        await streams.DataWriter(stream).write_bytes_async(data)
        decoder = await __import__(
            "winsdk.windows.graphics.imaging", fromlist=["BitmapDecoder"]
        ).BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    return asyncio.run(run()).strip()


def recognize(
    image, folder, engine_id, cancelled=lambda: False, backend=None, api_config=None
):
    backend = backend or engine_id
    api_config = api_config or {}
    if backend in {"api_openai", "api_gemini", "api_custom"}:
        if not api_config:
            raise PipelineError(
                "MODEL_API_NOT_CONFIGURED",
                "该 OCR 模型需要配置 API：请在模型中心点「配置 API」",
                409,
            )
        if backend == "api_openai":
            return _ocr_openai(image, api_config)
        if backend == "api_gemini":
            return _ocr_gemini(image, api_config)
        return _ocr_custom(image, api_config)
    if backend == "winrt":
        return _ocr_winrt(image)
    if backend == "paddle":
        try:
            from paddleocr import PaddleOCR
        except ImportError as error:
            raise PipelineError(
                "MODEL_DEPENDENCY_MISSING",
                "PaddleOCR 需要先安装：pip install paddleocr paddlepaddle",
                409,
            ) from error
        engine = _cached_engine(
            "paddle",
            lambda: PaddleOCR(use_angle_cls=True, lang="japan", show_log=False),
        )
        result = engine.ocr(np.asarray(image.convert("RGB")), cls=True)
        texts = [
            line[1][0]
            for block in (result or [])
            for line in (block or [])
            if isinstance(line, list) and len(line) > 1
        ]
        return "\n".join(texts).strip()
    if backend == "easyocr":
        try:
            import easyocr
        except ImportError as error:
            raise PipelineError(
                "MODEL_DEPENDENCY_MISSING",
                "EasyOCR 需要先安装：pip install easyocr",
                409,
            ) from error
        reader = _cached_engine(
            "easyocr", lambda: easyocr.Reader(["ja", "en"], gpu=False)
        )
        texts = reader.readtext(np.asarray(image.convert("RGB")), detail=0)
        return "\n".join(texts).strip()
    if backend == "doctr":
        try:
            from doctr.io import DocumentFile
            from doctr.models import ocr_predictor
        except ImportError as error:
            raise PipelineError(
                "MODEL_DEPENDENCY_MISSING",
                "DocTR 需要先安装：pip install python-doctr",
                409,
            ) from error
        from io import BytesIO

        buffer = BytesIO()
        image.convert("RGB").save(buffer, "PNG")
        predictor = _cached_engine(
            "doctr", lambda: ocr_predictor(pretrained=True)
        )
        result = predictor([np.asarray(image.convert("RGB"))])
        words = []

        def walk(node):
            if isinstance(node, dict):
                if "value" in node:
                    words.append(node["value"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for page in result.pages:
            walk(page.export())
        return " ".join(words).strip()
    if backend == "rapidocr" or engine_id == "rapidocr":
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
    if backend != "mangaocr":
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
    operation,
    image_path,
    folder,
    model_id,
    confidence=0.5,
    rect=None,
    render_data=None,
    backend=None,
    api_config=None,
):
    """Picklable process boundary: model crashes cannot terminate the API/Qt process."""
    from pathlib import Path

    if operation == "render":
        from Backend.domain.models.manga import MangaPage
        from Backend.infrastructure.manga.models import ModelRegistry
        from Backend.infrastructure.manga.render import materialize

        page = MangaPage.model_validate(render_data["page"])
        if not render_data.get("erase_untranslated"):
            for region in page.regions:
                if not region.target.strip() and not region.erase_only:
                    region.removal = "none"
        return materialize(
            page,
            Path(image_path),
            Path(render_data["directory"]),
            render_data["revision"],
            ModelRegistry(root=folder or None, bundled=render_data["bundled"] or None),
            background_key=render_data.get("background_key"),
            background_only=render_data.get("background_only", False),
            punctuation_rules=render_data.get("punctuation_rules"),
        )
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        if operation == "detect":
            return detect(image, Path(folder), model_id, confidence)
        if operation == "ocr":
            if rect:
                x, y, w, h = rect
                image = image.crop((int(x), int(y), int(x + w), int(y + h)))
            return recognize(
                image,
                Path(folder) if folder else Path("."),
                model_id,
                backend=backend,
                api_config=api_config,
            )
    raise ValueError("未知模型操作")
