"""Native CPU detection and recognition; runs inside this process."""

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
        # OpenCV 的 darknet 导入器按 ANSI 打开文件，路径含非 ASCII 字符时
        # 会报 "Failed to open NetParameter file"；改用字节缓冲避开路径编码。
        cfg_bytes = np.frombuffer((folder / "model.cfg").read_bytes(), dtype=np.uint8)
        weights_bytes = np.frombuffer(
            (folder / "model.weights").read_bytes(), dtype=np.uint8
        )
        net = _cached_engine(
            f"darknet:{folder}:{params.get('width')}x{params.get('height')}",
            lambda: cv2.dnn.readNetFromDarknet(cfg_bytes, weights_bytes),
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
    # YOLO letterbox preprocessing: RGB float CHW, end-to-end detections.
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


_BAIDU_TOKENS = {}  # appid -> (token, expire_at)，进程内缓存避免重复换 token


def _baidu_token(config):
    import time

    import requests

    cache_key = (config.get("appid", ""), config.get("base_url", ""))
    cached = _BAIDU_TOKENS.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    response = requests.get(
        (config.get("base_url") or "https://aip.baidubce.com").rstrip("/")
        + "/oauth/2.0/token",
        params={
            "grant_type": "client_credentials",
            "client_id": config.get("appid", ""),
            "client_secret": config.get("api_key", ""),
        },
        timeout=(10, 20),
    )
    response.raise_for_status()
    payload = response.json() or {}
    token = payload.get("access_token")
    if not token:
        raise PipelineError(
            "MODEL_API_AUTH",
            "百度 OCR 换取 access_token 失败，请检查 AppID 与 API Key",
            409,
        )
    expires = int(payload.get("expires_in") or 2592000)
    _BAIDU_TOKENS[cache_key] = (token, time.time() + expires)
    return token


def _tencent_call(config, image_bytes, action="GeneralBasicOCR", probe=False):
    """腾讯云 OCR：TC3-HMAC-SHA256 签名调用。"""
    import hashlib
    import hmac
    import json as jsonlib
    import time

    import requests

    secret_id = config.get("appid", "")
    secret_key = config.get("api_key", "")
    if not secret_id or not secret_key:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "腾讯 OCR 需要 SecretId 与 SecretKey：请在模型中心填写",
            409,
        )
    host = "ocr.tencentcloudapi.com"
    service = "ocr"
    region = config.get("region") or "ap-guangzhou"
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    payload = jsonlib.dumps(
        {"ImageBase64": (image_bytes and __import__("base64").b64encode(image_bytes).decode()) or ""}
    )
    hashed_payload = hashlib.sha256(payload.encode()).hexdigest()
    canonical_headers = (
        "content-type:application/json; charset=utf-8\nhost:" + host + "\n"
    )
    signed_headers = "content-type;host"
    canonical_request = (
        "POST\n/\n\n"
        + canonical_headers
        + "\n"
        + signed_headers
        + "\n"
        + hashed_payload
    )
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        + str(timestamp)
        + "\n"
        + credential_scope
        + "\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def sign(key, message):
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    secret_date = sign(("TC3" + secret_key).encode(), date)
    secret_service = sign(secret_date, service)
    secret_signing = sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    response = requests.post(
        f"https://{host}",
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": "2018-11-19",
            "X-TC-Region": region,
        },
        data=payload.encode(),
        timeout=(10, 60),
    )
    body = response.json() if response.content else {}
    if probe:
        if response.status_code == 200 and not (body.get("Response") or {}).get("Error"):
            return True
        error = (body.get("Response") or {}).get("Error") or {}
        raise PipelineError(
            "MODEL_API_AUTH",
            "腾讯 OCR 鉴权失败：" + str(error.get("Message") or response.status_code),
            409,
        )
    response.raise_for_status()
    error = (body.get("Response") or {}).get("Error")
    if error:
        raise PipelineError(
            "MODEL_API_ERROR",
            "腾讯 OCR 返回错误：" + str(error.get("Message", "")),
            409,
        )
    return body


def _ocr_tencent(image, config):
    from io import BytesIO

    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    body = _tencent_call(config, buffer.getvalue())
    detections = (body.get("Response") or {}).get("TextDetections") or []
    return "\n".join(str(item.get("DetectedText", "")) for item in detections).strip()


def _ocr_clova(image, config):
    import base64
    import time
    from io import BytesIO
    from uuid import uuid4

    import requests

    url = config.get("base_url", "")
    secret = config.get("api_key", "")
    if not url or not secret:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "CLOVA OCR 需要 Invoke URL 与 Secret Key：请在模型中心填写",
            409,
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    response = requests.post(
        url,
        headers={"X-OCR-SECRET": secret, "Content-Type": "application/json"},
        json={
            "version": "V2",
            "requestId": uuid4().hex,
            "timestamp": int(time.time() * 1000),
            "images": [
                {
                    "format": "png",
                    "name": "page",
                    "data": base64.b64encode(buffer.getvalue()).decode(),
                }
            ],
        },
        timeout=(10, 60),
    )
    response.raise_for_status()
    payload = response.json() or {}
    images = payload.get("images") or []
    fields = images[0].get("fields") if images else []
    return "\n".join(
        str(item.get("inferText", "")) for item in (fields or [])
    ).strip()


def _ocr_abbyy(image, config):
    from io import BytesIO

    import requests

    appid = config.get("appid", "")
    password = config.get("api_key", "")
    if not appid or not password:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "ABBYY OCR 需要 Application ID 与密码",
            409,
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    language = config.get("model") or "Japanese,Chinese,English"
    response = requests.post(
        (config.get("base_url") or "https://ocrsdk.com").rstrip("/")
        + "/processImage",
        params={"language": language, "exportFormat": "txt"},
        files={"image": ("image.png", buffer.getvalue(), "image/png")},
        auth=(appid, password),
        timeout=(10, 120),
    )
    response.raise_for_status()
    return (response.text or "").strip()


def _ocr_google(image, config):
    import base64
    from io import BytesIO

    import requests

    key = config.get("api_key", "")
    if not key:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED", "Google Vision 需要 API Key", 409
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    response = requests.post(
        (config.get("base_url") or "https://vision.googleapis.com").rstrip("/")
        + "/v1/images:annotate",
        params={"key": key},
        json={
            "requests": [
                {
                    "image": {
                        "content": base64.b64encode(buffer.getvalue()).decode()
                    },
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        },
        timeout=(10, 60),
    )
    response.raise_for_status()
    responses = (response.json() or {}).get("responses") or []
    if not responses:
        return ""
    full = responses[0].get("fullTextAnnotation") or {}
    return str(full.get("text", "")).strip()


def _ocr_ocrspace(image, config):
    import base64
    from io import BytesIO

    import requests

    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    response = requests.post(
        (config.get("base_url") or "https://api.ocr.space").rstrip("/")
        + "/parse/image",
        headers={"apikey": config.get("api_key") or "helloworld"},
        data={
            "base64Image": "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode(),
            "language": config.get("model") or "jpn",
            "OCREngine": "3",
        },
        timeout=(10, 60),
    )
    response.raise_for_status()
    payload = response.json() or {}
    parsed = payload.get("ParsedResults") or []
    return "\n".join(
        str(item.get("ParsedText", "")) for item in (parsed or [])
    ).strip()


def _ocr_youdao(image, config):
    import base64
    import hashlib
    import time
    from io import BytesIO
    from uuid import uuid4

    import requests

    appid = config.get("appid", "")
    secret = config.get("api_key", "")
    if not appid or not secret:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "有道 OCR 需要应用 ID 与应用密钥：请在模型中心填写",
            409,
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    payload = base64.b64encode(buffer.getvalue()).decode()
    salt = str(uuid4().int % 100000)
    curtime = str(int(time.time()))
    sign = hashlib.sha256(
        (appid + payload + salt + curtime + secret).encode()
    ).hexdigest()
    response = requests.post(
        (config.get("base_url") or "https://openapi.youdao.com").rstrip("/")
        + "/ocrapi",
        data={
            "img": payload,
            "langType": config.get("model") or "ja",
            "detectType": "10012",
            "imageType": "1",
            "appKey": appid,
            "docType": "json",
            "signType": "v3",
            "curtime": curtime,
            "salt": salt,
            "sign": sign,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(10, 60),
    )
    response.raise_for_status()
    body = response.json() or {}
    if str(body.get("errorCode", "0")) != "0":
        raise PipelineError(
            "MODEL_API_ERROR",
            f"有道 OCR 返回错误 {body.get('errorCode')}",
            409,
        )
    result = body.get("Result") or {}
    lines = []
    for region in result.get("regions") or []:
        for line in region.get("lines") or []:
            if isinstance(line, dict) and line.get("text"):
                lines.append(str(line["text"]))
    if not lines and isinstance(result.get("text"), str):
        lines.append(result["text"])
    return "\n".join(lines).strip()


def _ocr_azure(image, config):
    import time
    from io import BytesIO

    import requests

    endpoint = (config.get("base_url") or "").rstrip("/")
    key = config.get("api_key", "")
    if not endpoint or not key:
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "Azure OCR 需要 Endpoint 与 Key：请在模型中心填写",
            409,
        )
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    response = requests.post(
        endpoint + (config.get("model") or "/vision/v2.0/ocr"),
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/octet-stream",
        },
        data=buffer.getvalue(),
        timeout=(10, 60),
    )
    response.raise_for_status()
    body = response.json() or {}
    lines = []
    # Azure v2.0 同步接口返回 regions[].lines[].words[].text
    for region in body.get("regions") or []:
        for line in region.get("lines") or []:
            words = " ".join(
                str(word.get("text", "")) for word in line.get("words") or []
            )
            if words.strip():
                lines.append(words.strip())
            elif line.get("text"):
                lines.append(str(line["text"]))
    return "\n".join(lines).strip()


def _ocr_ollama(image, config):
    import base64
    from io import BytesIO

    import requests

    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    payload = base64.b64encode(buffer.getvalue()).decode()
    response = requests.post(
        (config.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        + "/api/generate",
        json={
            "model": config.get("model") or "llava",
            "prompt": "只输出图片中的文字，不要解释，不要加标点解释。",
            "images": [payload],
            "stream": False,
        },
        timeout=(10, 180),
    )
    response.raise_for_status()
    return str((response.json() or {}).get("response", "")).strip()


def _ocr_tesseract(image, config):
    import shutil
    import subprocess
    from io import BytesIO

    exe = config.get("base_url") or shutil.which("tesseract") or "tesseract"
    language = config.get("model") or "jpn"
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    try:
        result = subprocess.run(
            [exe, "stdin", "stdout", "-l", language, "--psm", "6"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PipelineError(
            "MODEL_DEPENDENCY_MISSING",
            f"未找到可用的 tesseract（{exe}）：请安装并把路径填入接口地址",
            409,
        ) from error
    if result.returncode != 0:
        raise PipelineError(
            "MODEL_API_ERROR",
            "tesseract 执行失败：" + result.stderr.decode("utf-8", "replace")[:160],
            409,
        )
    return result.stdout.decode("utf-8", "replace").strip()


def _ocr_mit48px(image, folder, config):
    """MIT48px CTC 行识别；需要导入模型与字符表目录。"""
    import onnxruntime as ort

    model_path = Path(folder) / "ocr_ctc_fp16.onnx"
    alphabet_path = Path(folder) / "alphabet-all-v5.txt"
    if not model_path.is_file() or not alphabet_path.is_file():
        raise PipelineError(
            "MODEL_IMPORT_REQUIRED",
            "请先导入 ocr_ctc_fp16.onnx 与 alphabet-all-v5.txt 所在目录",
            409,
        )
    try:
        session_ = ort.InferenceSession(str(model_path))
    except Exception as error:
        raise PipelineError(
            "MODEL_UNSUPPORTED",
            "该 CTC 权重与当前 onnxruntime 版本不兼容，请更换推理环境",
            422,
        ) from error
    alphabet = alphabet_path.read_text(encoding="utf-8", errors="replace").splitlines()
    gray = image.convert("L")
    height = 48
    width = max(8, round(gray.width * height / max(1, gray.height)))
    resized = gray.resize((width, height))
    tensor = (np.asarray(resized, dtype=np.float32) / 255.0)[None, None]
    try:
        logits = np.asarray(
            session_.run(None, {session_.get_inputs()[0].name: tensor})[0]
        )
    except Exception as error:
        raise PipelineError("MODEL_OUTPUT_MISMATCH", str(error), 422) from error
    logits = logits.reshape(-1, logits.shape[-1])
    indices = logits.argmax(axis=1)
    chars, last = [], None
    for index in indices:
        index = int(index)
        if index == last or index == 0:
            last = index
            continue
        last = index
        if index < len(alphabet):
            chars.append(alphabet[index])
    return "".join(chars).strip()


def _ocr_baidu(image, config):
    import base64
    from io import BytesIO

    import requests

    if not config.get("appid") or not config.get("api_key"):
        raise PipelineError(
            "MODEL_API_NOT_CONFIGURED",
            "百度 OCR 需要 AppID 与 API Key：请在模型中心填写后再用",
            409,
        )
    token = _baidu_token(config)
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    # 模式映射：accurate / accurateWithLocation /
    # general / withLocation / network(webimage)
    endpoint = {
        "accurate": "/rest/2.0/ocr/v1/accurate_basic",
        "accuratewithlocation": "/rest/2.0/ocr/v1/accurate",
        "accurateWithLocation": "/rest/2.0/ocr/v1/accurate",
        "general": "/rest/2.0/ocr/v1/general_basic",
        "withlocation": "/rest/2.0/ocr/v1/general",
        "network": "/rest/2.0/ocr/v1/webimage",
    }.get(config.get("model") or "accurate", "/rest/2.0/ocr/v1/accurate_basic")
    response = requests.post(
        (config.get("base_url") or "https://aip.baidubce.com").rstrip("/")
        + endpoint,
        params={"access_token": token},
        data={"image": data, "detect_direction": "true"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(10, 60),
    )
    response.raise_for_status()
    payload = response.json() or {}
    if payload.get("error_code"):
        raise PipelineError(
            "MODEL_API_ERROR",
            f"百度 OCR 返回错误 {payload.get('error_code')}："
            + str(payload.get("error_msg", "")),
            409,
        )
    results = payload.get("words_result") or []
    return "\n".join(str(item.get("words", "")) for item in results).strip()


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
    if backend == "mit48px":
        return _ocr_mit48px(image, folder, api_config or {})
    if backend == "tesseract":
        return _ocr_tesseract(image, api_config or {})
    if backend in {
        "api_openai",
        "api_gemini",
        "api_baidu",
        "api_tencent",
        "api_clova",
        "api_abbyy",
        "api_google",
        "api_ocrspace",
        "api_youdao",
        "api_azure",
        "api_ollama",
        "api_custom",
    }:
        # Ollama 是本地推理，缺省即可用（127.0.0.1:11434），其余需要凭据
        if not api_config and backend != "api_ollama":
            raise PipelineError(
                "MODEL_API_NOT_CONFIGURED",
                "该 OCR 模型需要配置 API：请在模型中心点「配置 API」",
                409,
            )
        if backend == "api_openai":
            return _ocr_openai(image, api_config)
        if backend == "api_gemini":
            return _ocr_gemini(image, api_config)
        if backend == "api_baidu":
            return _ocr_baidu(image, api_config)
        if backend == "api_tencent":
            return _ocr_tencent(image, api_config)
        if backend == "api_clova":
            return _ocr_clova(image, api_config)
        if backend == "api_abbyy":
            return _ocr_abbyy(image, api_config)
        if backend == "api_google":
            return _ocr_google(image, api_config)
        if backend == "api_ocrspace":
            return _ocr_ocrspace(image, api_config)
        if backend == "api_youdao":
            return _ocr_youdao(image, api_config)
        if backend == "api_azure":
            return _ocr_azure(image, api_config)
        if backend == "api_ollama":
            return _ocr_ollama(image, api_config)
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
