"""Versioned public model catalogue and verified, non-executable asset imports."""

import sys
from pathlib import Path
import hashlib
import json
import os
import shutil
import zipfile
import requests
from Backend.application.pipeline_common import PipelineError
from Backend.application.manga_store import atomic_json


_SYSTEM_OCR_AVAILABLE = sys.platform == "win32"
LEGACY_BALLOON_ID = "legacy-balloon-yolov4"
LEGACY_BALLOON_HASHES = {
    "model.weights": "7bd9c3b91c6c042efbb88115a9e2c495a167d5622da5b45da83a4cceafb82662",
    "model.cfg": "1584e064f6ff8c32c63621a2974cc920f0bd4bc670cf87ca36a2dd7b398963c3",
}

CATALOGUE = [
    # ================= 检测模型（ONNX + model.json 描述文件）=================
    dict(
        id="manga-panel",
        name="Manga panel YOLO26",
        role="detect",
        kind="local",
        backend="yolo26",
        default_confidence=0.25,
        core=True,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja", "zh", "ko", "en"],
        files=["model.onnx", "model.json"],
        license="AGPL-3.0",
        size_mb=10,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/manga-panel.zip",
        source="公开漫画检测权重仓库",
        advice="轻量日漫文字定位；分镜类别与文字类别分别处理。内置随包提供",
    ),
    dict(
        id="ppocr-manga-det",
        name="PP-OCRv6 漫画行检测",
        role="detect",
        kind="local",
        backend="rapidocr_det",
        default_confidence=0.5,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.onnx", "model.json"],
        license="Apache-2.0",
        size_mb=2,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/PPOCRv6-manga-det.zip",
        source="公开漫画检测权重仓库",
        advice="最轻量的日文行检测；按 PP-OCR det 分割推理，适合快速分框与试跑",
    ),
    dict(
        id="japanese-lines",
        name="Japanese lines 行检测",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=True,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=11,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/japanese-lines.zip",
        source="公开漫画检测权重仓库",
        advice=(
            "日文竖排行级检测，随包内置（11 MB）。覆盖稳定，面板模型只出"
            "整页面板框时会自动用它补齐文字框；也可直接作为默认检测器"
        ),
    ),
    dict(
        id="japanese-manga",
        name="Japanese manga 气泡检测",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=33,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/japanese-manga.zip",
        source="公开漫画检测权重仓库",
        advice="日漫气泡定位主力模型；框更贴合气泡边缘，减少误检背景文字",
    ),
    dict(
        id="chinese-webtoon",
        name="Chinese webtoon 条漫检测",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["zh"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=11,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/chinese_webtoon.zip",
        source="公开漫画检测权重仓库",
        advice="国漫条漫（长条竖版）文字气泡检测",
    ),
    dict(
        id="korean-webtoon",
        name="Korean webtoon 条漫检测",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ko"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=11,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/korean_webtoon.zip",
        source="公开漫画检测权重仓库",
        advice="韩漫条漫文字气泡检测",
    ),
    dict(
        id="comic-speech-bubble",
        name="Comic speech bubble 气泡检测",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["en"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=85,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/comic-speech-bubble.zip",
        source="公开漫画检测权重仓库",
        advice="欧美漫画对话气泡检测（英文漫画优先）",
    ),
    dict(
        id="manga-with-layout",
        name="Manga Layout 1152 · Koharu 精细版",
        role="detect",
        kind="local",
        backend="layout",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja", "zh", "ko", "en"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=78,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/manga-with-layout.zip",
        source="balloon-dataset / mayocream Koharu",
        docs_url="https://github.com/xulihang/balloon-dataset#readme",
        advice="定位正文、拟声词和分镜；AiNiee 使用检测框，暂不使用该权重的像素蒙版",
        recommendation="推荐重跑漏小字或拟声词的单页；CPU 耗时较长，整部批量优先用 768 快速版。",
    ),
    dict(
        id="japanese-manga-old",
        name="Japanese manga（旧版权重）",
        role="detect",
        kind="local",
        backend="yolo_v8",
        default_confidence=0.25,
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=214,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/japanese-manga-old.zip",
        source="公开漫画检测权重仓库",
        advice="旧版日漫检测权重（214MB），仅在对比历史结果时使用；高级/可选",
    ),
    dict(
        id="manga-with-layout-768",
        name="Manga Layout 768 · Koharu 快速版",
        role="detect",
        kind="local",
        backend="layout",
        default_confidence=0.25,
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja", "zh", "ko", "en"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=66,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/manga-with-layout-768px.zip",
        source="balloon-dataset / DevilishDaoSaint Koharu",
        docs_url="https://huggingface.co/DevilishDaoSaint/koharu-layout-rfdetr",
        advice="定位正文和分镜；不输出像素蒙版，当前配置跳过拟声词",
        recommendation="推荐 CPU 批量检测日漫；如果漏掉小字或拟声词，用 1152 精细版复查。",
    ),
    dict(
        id="rfdetr",
        name="RF-DETR 512 · 日漫单行检测",
        role="detect",
        kind="local",
        backend="rfdetr",
        default_confidence=0.25,
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.onnx", "model.json"],
        license="见原仓库",
        size_mb=106,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/rfdetr.zip",
        source="公开漫画检测权重仓库",
        docs_url="https://github.com/xulihang/ImageTrans-docs/issues/1069",
        advice="检测日漫和 CG 的文字行，编辑器会尝试把同一气泡里的行合并",
        recommendation="推荐在气泡模型漏掉狭长竖排文字时对比使用，定位后仍需运行 OCR。",
    ),
    dict(
        id=LEGACY_BALLOON_ID,
        name="ImageTrans Legacy 气泡（Scaled YOLOv4）",
        role="detect",
        kind="local",
        backend="yolov4",
        default_confidence=0.20,
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["model.weights", "model.cfg", "model.json"],
        license="见原权重说明",
        size_mb=281,
        url="",
        source="ImageTrans 旧版模型目录",
        docs_url="https://github.com/xulihang/balloon-dataset#use-the-models-in-imagetrans",
        advice="仅检测气泡；512×512，默认置信度 0.20，NMS 0.50；需从 ImageTrans 导入原版文件",
        recommendation="推荐旧版日漫或新版模型误把背景当气泡时对比使用；不检测分镜，且可能漏掉无气泡文字。",
    ),
    # ================= 本地 OCR（pip 模型 / ONNX 权重）=================
    dict(
        id="mangaocr",
        name="mangaOCR ONNX",
        role="ocr",
        kind="local",
        backend="mangaocr",
        core=True,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["encoder.onnx", "decoder.onnx", "vocab.txt"],
        license="Apache-2.0",
        size_mb=120,
        url="https://github.com/xulihang/mangaocr-onnx/releases/download/models/",
        source="公开 OCR 权重仓库",
        advice="日语漫画单框识字，横排竖排均可；不负责定位。内置随包提供",
    ),
    dict(
        id="rapidocr",
        name="RapidOCR PP-OCR",
        role="ocr",
        kind="local",
        backend="rapidocr",
        core=True,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["zh", "ja", "ko", "en"],
        files=["det.onnx", "rec.onnx", "cls.onnx"],
        license="Apache-2.0",
        size_mb=20,
        url="",
        source="https://github.com/RapidAI/RapidOCR",
        advice="通用检测与中英日韩识别；内置随包提供，可导入其他语种识别权重",
    ),
    dict(
        id="paddleocr",
        name="PaddleOCR PP-OCRv5",
        role="ocr",
        kind="package",
        backend="paddle",
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["zh", "ja", "ko", "en"],
        files=[],
        license="Apache-2.0",
        size_mb=50,
        url="",
        source="https://github.com/PaddlePaddle/PaddleOCR",
        advice="PaddleOCR 全语言识别；首次使用时由 pip 包自动下载模型（需安装 paddleocr 与 paddlepaddle）",
        supported=True,
    ),
    dict(
        id="easyocr",
        name="EasyOCR",
        role="ocr",
        kind="package",
        backend="easyocr",
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["zh", "ja", "ko", "en"],
        files=[],
        license="Apache-2.0",
        size_mb=100,
        url="",
        source="https://github.com/JaidedAI/EasyOCR",
        advice="80+ 语言通用 OCR；首次使用时自动下载所选语言模型（需安装 easyocr）",
    ),
    dict(
        id="doctr",
        name="DocTR（拉丁语系）",
        role="ocr",
        kind="package",
        backend="doctr",
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["en"],
        files=[],
        license="Apache-2.0",
        size_mb=50,
        url="",
        source="https://github.com/mindee/doctr",
        advice="拉丁语系文字识别；首次使用时自动下载权重（需安装 python-doctr）",
    ),
    # ================= 云 API OCR（需要配置 API）=================
    dict(
        id="api-openai",
        name="ChatGPT OCR（OpenAI 兼容）",
        role="ocr",
        kind="api",
        backend="api_openai",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="openai",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://platform.openai.com/docs",
        advice="用视觉大模型识字，跨语言准确；也可填任意 OpenAI 兼容网关地址",
    ),
    dict(
        id="api-gemini",
        name="Gemini OCR",
        role="ocr",
        kind="api",
        backend="api_gemini",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="gemini",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://ai.google.dev/gemini-api/docs",
        advice="Google Gemini 视觉识字；适合多语言混排与手写体",
    ),
    dict(
        id="youdao-ocr",
        name="有道智云 OCR",
        role="ocr",
        kind="api",
        backend="api_youdao",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="youdao",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://ai.youdao.com",
        advice="有道智云通用文字识别（v3 签名）；需应用 ID 与应用密钥",
    ),
    dict(
        id="azure-ocr",
        name="Azure Computer Vision OCR",
        role="ocr",
        kind="api",
        backend="api_azure",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="azure",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://learn.microsoft.com/azure/ai-services/computer-vision/",
        advice="Azure 计算机视觉 OCR（v2.0 同步接口）；需 Endpoint 与 Key",
    ),
    dict(
        id="ollama-ocr",
        name="Ollama 视觉模型 OCR",
        role="ocr",
        kind="api",
        backend="api_ollama",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="ollama",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="随本地模型",
        size_mb=0,
        url="",
        source="https://ollama.com",
        advice="本地 Ollama 视觉模型识字（无需云 key）；默认 127.0.0.1:11434，模型如 llava / qwen2.5vl",
    ),
    dict(
        id="tesseract-ocr",
        name="Tesseract OCR",
        role="ocr",
        kind="system",
        backend="tesseract",
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="Apache-2.0",
        size_mb=0,
        url="",
        source="https://github.com/tesseract-ocr/tesseract",
        advice="调用本机 tesseract 可执行与语言包；未安装时会提示安装路径",
        supported=True,
    ),
    dict(
        id="mit48px-ctc",
        name="MIT48px CTC 行识别",
        role="ocr",
        kind="local",
        backend="mit48px",
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=["ja"],
        files=["ocr_ctc_fp16.onnx", "alphabet-all-v5.txt"],
        license="见 Manga-Text-Segmentation",
        size_mb=83,
        url="",
        source="https://github.com/ljsabc/Manga-Text-Segmentation",
        advice="日文单行 CTC 识别；请导入 ocr_ctc_fp16.onnx 与 alphabet 字符表目录",
    ),
    dict(
        id="mangatranslator-ocr",
        name="MangaTranslator OCR",
        role="ocr",
        kind="api",
        backend="api_mangatranslator",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="mangatranslator",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="见项目说明",
        size_mb=0,
        url="",
        source="自建识别服务",
        advice="一体式漫画识别服务（需自建）：填本地服务地址，默认 127.0.0.1:8080",
    ),
    dict(
        id="zxingcpp-ocr",
        name="ZXing 条码识别",
        role="ocr",
        kind="system",
        backend="zxingcpp",
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=[],
        files=[],
        license="Apache-2.0",
        size_mb=0,
        url="",
        source="https://github.com/zxing-cpp/zxing-cpp",
        advice="识别二维码/条形码文本（非对白 OCR）；高级/可选",
        supported=False,
    ),
    dict(
        id="baidu-network",
        name="百度网络图片 OCR（webimage）",
        role="ocr",
        kind="api",
        backend="api_baidu",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="baidu",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://cloud.baidu.com/doc/OCR/s/8k3h7xt6p",
        advice="百度网络图片文字识别（webimage），与百度 OCR 共用 AppID/Key；模型名填 network",
    ),
    dict(
        id="api-ocrspace",
        name="OCR.space（免费额度）",
        role="ocr",
        kind="api",
        backend="api_ocrspace",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="ocrspace",
        free_key="helloworld",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款（免费额度）",
        size_mb=0,
        url="",
        source="https://ocr.space/ocrapi",
        advice="免费额度的通用 OCR；免费测试 Key 填 helloworld，付费 Key 额度更高",
    ),
    dict(
        id="api-glm",
        name="GLM-4V OCR",
        role="ocr",
        kind="api",
        backend="api_openai",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="glm",
        langs=["zh", "ja", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://open.bigmodel.cn",
        advice="智谱 GLM 视觉模型；使用 OpenAI 兼容接口",
    ),
    dict(
        id="api-deepseek",
        name="DeepSeek OCR",
        role="ocr",
        kind="api",
        backend="api_openai",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="deepseek",
        langs=["zh", "ja", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://platform.deepseek.com",
        advice="DeepSeek 兼容接口识字；需确认所用模型支持图片输入",
    ),
    dict(
        id="api-baidu",
        name="百度 OCR",
        role="ocr",
        kind="api",
        backend="api_baidu",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="baidu",
        langs=["zh", "ja", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://cloud.baidu.com/product/ocr",
        advice="百度高精度 OCR（accurate_basic + 方向检测），需 AppID 与 API Key，token 自动缓存",
    ),
    dict(
        id="api-tencent",
        name="腾讯 OCR",
        role="ocr",
        kind="api",
        backend="api_tencent",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="tencent",
        langs=["zh", "ja", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://cloud.tencent.com/product/ocr",
        advice="腾讯云通用文字识别（GeneralBasicOCR，TC3 签名）；需 SecretId 与 SecretKey",
    ),
    dict(
        id="api-sogou",
        name="搜狗 OCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="sogou",
        langs=["zh", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://ai.sogou.com",
        advice="搜狗 OCR；需服务商凭证，签名协议由兼容网关接入",
    ),
    dict(
        id="api-clova",
        name="Naver CLOVA OCR",
        role="ocr",
        kind="api",
        backend="api_clova",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="clova",
        langs=["ko", "ja", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://www.ncloud.com/product/aiService/ocr",
        advice="Naver CLOVA 通用 OCR（V2）；需 Invoke URL 与 X-OCR-SECRET",
    ),
    dict(
        id="api-abbyy",
        name="ABBYY Cloud OCR",
        role="ocr",
        kind="api",
        backend="api_abbyy",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="abbyy",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://www.abbyy.com/cloud-ocr-sdk",
        advice="ABBYY Cloud OCR（processImage → txt）；需 Application ID 与密码",
    ),
    dict(
        id="api-google",
        name="Google Cloud Vision OCR",
        role="ocr",
        kind="api",
        backend="api_google",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="google",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://cloud.google.com/vision/docs/ocr",
        advice="Google Cloud Vision（DOCUMENT_TEXT_DETECTION）；需 API Key",
    ),
    dict(
        id="api-googledrive",
        name="Google Drive OCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=False,
        requires_api=True,
        api_provider="googledrive",
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://developers.google.com/drive",
        advice="借 Drive 文档转换做 OCR；需 OAuth 访问令牌",
    ),
    dict(
        id="api-kandianguji",
        name="Kandian Guji 古籍 OCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="kandianguji",
        langs=["zh"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://www.kandianguji.com",
        advice="竖排古籍/繁体识别；高级/可选",
    ),
    dict(
        id="api-dynamsoft",
        name="Dynamsoft OCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="dynamsoft",
        langs=["zh", "ja", "ko", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://www.dynamsoft.com",
        advice="商业 OCR SDK；高级/可选",
    ),
    dict(
        id="api-mineru",
        name="MinerU OCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="mineru",
        langs=["zh", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://github.com/opendatalab/MinerU",
        advice="文档解析型 OCR；高级/可选",
    ),
    dict(
        id="api-panda",
        name="PandaOCR",
        role="ocr",
        kind="api",
        backend="api_custom",
        core=False,
        advanced=True,
        requires_api=True,
        api_provider="panda",
        langs=["zh", "en"],
        files=[],
        license="按服务商条款",
        size_mb=0,
        url="",
        source="https://github.com/RapidAI/PandaOCR",
        advice="聚合型 OCR 前端；高级/可选，需自建服务端",
    ),
    # ================= 系统 OCR（Windows 原生）=================
    dict(
        id="winrtocr",
        name="Windows 系统 OCR（WinRT）",
        role="ocr",
        kind="system",
        backend="winrt",
        core=False,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=["ja", "zh", "ko", "en"],
        files=[],
        license="跟随 Windows 语言包",
        size_mb=0,
        url="",
        source="https://learn.microsoft.com/uwp/api/windows.media.ocr",
        advice="调用 Windows.Media.Ocr，需在系统设置安装对应语言包；无需下载模型",
        supported=_SYSTEM_OCR_AVAILABLE,
    ),
    dict(
        id="oneocr",
        name="OneNote OCR（oneOCR）",
        role="ocr",
        kind="system",
        backend="oneocr",
        core=False,
        advanced=True,
        requires_api=False,
        api_provider=None,
        langs=["zh", "ja", "ko", "en"],
        files=[],
        license="跟随 Windows/Office",
        size_mb=0,
        url="",
        source="系统组件",
        advice="调用 OneNote 组件识字；高级/可选，需本机装有 OneNote 引擎",
        supported=False,
    ),
    # ================= 修复 / Inpainting =================
    dict(
        id="lama",
        name="LaMa Manga ONNX",
        role="inpaint",
        kind="local",
        backend="lama",
        core=True,
        advanced=False,
        requires_api=False,
        api_provider=None,
        langs=[],
        files=["model.onnx"],
        license="Apache-2.0",
        size_mb=208,
        url="https://huggingface.co/mayocream/lama-manga-onnx/resolve/main/lama-manga.onnx",
        source="https://huggingface.co/mayocream/lama-manga-onnx",
        advice="复杂背景精准去字；需有效的文字像素蒙版。内置随包提供",
    ),
]


class ModelRegistry:
    def __init__(self, root=None, bundled=None):
        from Backend.core.paths import user_data_root, backend_resource_path

        self.root = Path(root) if root else user_data_root() / "manga-models"
        self.bundled = (
            Path(bundled) if bundled else backend_resource_path("manga", "models")
        )

    def spec(self, model_id):
        for spec in CATALOGUE:
            if spec["id"] == model_id:
                return spec
        # 兼容旧工程包与旧界面把显示名写进设置的情况
        if model_id:
            for spec in CATALOGUE:
                if spec.get("name") == model_id:
                    return spec
        raise PipelineError(
            "UNKNOWN_MANGA_MODEL",
            f"未知漫画模型：{model_id}；请在项目设置里重新选择检测或 OCR 模型",
            422,
        )

    def folder(self, model_id):
        spec = self.spec(model_id)
        for root in (self.root, self.bundled):
            folder = root / model_id
            if all((folder / f).is_file() for f in spec["files"]):
                return folder
        raise PipelineError(
            "MANGA_MODEL_MISSING", f"请在模型中心安装或导入 {model_id}", 409
        )

    # ---- 云 API OCR 配置（独立于工程数据，存用户目录）----
    def api_config_path(self):
        return self.root.parent / "manga-ocr-apis.json"

    def api_configs(self):
        path = self.api_config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_api_config(self, provider, config):
        apis = self.api_configs()
        cleaned = {
            k: v
            for k, v in (config or {}).items()
            if isinstance(v, str) and v.strip()
        }
        if cleaned:
            apis[provider] = cleaned  # 改动配置即失效，需重新验证
        else:
            apis.pop(provider, None)
        path = self.api_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, apis)
        return self.catalogue()

    def api_config(self, provider):
        return self.api_configs().get(provider or "", {})

    def _probe_api(self, backend, config):
        """用一次轻量请求验证云 OCR 配置是否可用。"""
        import requests

        key = config.get("api_key", "")
        base = (config.get("base_url") or "").rstrip("/")
        if backend == "api_openai":
            url = (base or "https://api.openai.com/v1") + "/models"
            response = requests.get(
                url,
                headers={"Authorization": "Bearer " + key} if key else {},
                timeout=(10, 20),
            )
        elif backend == "api_gemini":
            url = (
                base or "https://generativelanguage.googleapis.com/v1beta"
            ) + "/models"
            response = requests.get(url, params={"key": key}, timeout=(10, 20))
        elif backend == "api_tencent":
            from Backend.infrastructure.manga.engines import _tencent_call

            try:
                _tencent_call(config, b"", action="GeneralBasicOCR", probe=True)
                return True, ""
            except PipelineError as error:
                return False, error.message
        elif backend == "api_clova":
            url = base
            if not url:
                return False, "CLOVA 需要填写 Invoke URL（接口地址）"
            response = requests.post(
                url,
                headers={"X-OCR-SECRET": key, "Content-Type": "application/json"},
                json={"version": "V2", "requestId": "probe", "timestamp": 0, "images": []},
                timeout=(10, 20),
            )
            if response.status_code < 500 and response.status_code != 401:
                return True, ""
            return False, f"CLOVA 返回 {response.status_code}，请检查 URL 与 Secret"
        elif backend == "api_abbyy":
            if not config.get("appid") or not key:
                return False, "ABBYY 需要 Application ID 与密码"
            response = requests.get(
                (base or "https://ocrsdk.com").rstrip("/") + "/getApplicationInfo",
                auth=(config.get("appid", ""), key),
                timeout=(10, 20),
            )
            if response.status_code == 200:
                return True, ""
            return False, f"ABBYY 鉴权失败（{response.status_code}）"
        elif backend == "api_google":
            if not key:
                return False, "Google Vision 需要 API Key"
            response = requests.post(
                (base or "https://vision.googleapis.com").rstrip("/")
                + "/v1/images:annotate",
                params={"key": key},
                json={"requests": []},
                timeout=(10, 20),
            )
            if response.status_code in (200, 400) and "error" not in str(
                response.text[:200]
            ).lower():
                return True, ""
            if response.status_code == 403:
                return False, "Google Vision API Key 无效或未启用该 API"
            return True, ""
        elif backend == "api_youdao":
            if not config.get("appid") or not key:
                return False, "有道 OCR 需要应用 ID 与应用密钥"
            token = hashlib.sha256(
                (config.get("appid", "") + key).encode()
            ).hexdigest()
            response = requests.get(
                (base or "https://openapi.youdao.com").rstrip("/") + "/api",
                params={
                    "q": "test",
                    "from": "en",
                    "to": "zh-CHS",
                    "appKey": config.get("appid", ""),
                    "salt": "1",
                    "sign": token[:32],
                },
                timeout=(10, 20),
            )
            body = response.json() if response.content else {}
            code = str(body.get("errorCode", ""))
            if code == "108":
                return False, "有道应用 ID 或密钥无效"
            return True, ""
        elif backend == "api_azure":
            if not base or not key:
                return False, "Azure OCR 需要 Endpoint 与 Key"
            response = requests.get(
                base.rstrip("/") + "/vision/v2.0/ocr",
                headers={"Ocp-Apim-Subscription-Key": key},
                params={"language": "unk", "detectOrientation": "true"},
                timeout=(10, 20),
            )
            if response.status_code in (401, 403):
                return False, "Azure Key 无效"
            return True, ""
        elif backend == "api_ollama":
            url = (base or "http://127.0.0.1:11434").rstrip("/")
            try:
                response = requests.get(url + "/api/tags", timeout=(5, 10))
            except Exception as error:
                return False, f"无法连接 Ollama：{error}"
            if response.status_code == 200:
                return True, ""
            return False, f"Ollama 返回 {response.status_code}"
        elif backend == "api_ocrspace":
            response = requests.post(
                (base or "https://api.ocr.space").rstrip("/") + "/parse/image",
                headers={"apikey": key or "helloworld"},
                data={"base64Image": "data:image/png;base64,iVBORw0KGgo="},
                timeout=(10, 20),
            )
            if response.status_code == 200:
                return True, ""
            return False, f"OCR.space 返回 {response.status_code}，请检查 Key"
        elif backend == "api_baidu":
            appid = config.get("appid", "")
            if not appid or not key:
                return False, "百度 OCR 需要同时填写 AppID 与 API Key"
            response = requests.get(
                base or "https://aip.baidubce.com/oauth/2.0/token",
                params={
                    "grant_type": "client_credentials",
                    "client_id": appid,
                    "client_secret": key,
                },
                timeout=(10, 20),
            )
            if response.status_code == 200 and "access_token" in (
                response.json() or {}
            ):
                return True, ""
            return False, "AppID 或 API Key 无效（换取 access_token 失败）"
        else:
            if not base:
                raise PipelineError(
                    "MODEL_API_ENDPOINT_REQUIRED",
                    "该服务需要兼容网关：请填写接口地址后再验证",
                    409,
                )
            response = requests.get(
                base,
                headers={"Authorization": "Bearer " + key} if key else {},
                timeout=(10, 20),
            )
        if response.status_code in (200, 201, 204):
            return True, ""
        if response.status_code in (401, 403):
            return False, f"鉴权失败（{response.status_code}），请检查 API Key"
        if response.status_code == 404:
            return False, "接口地址不存在（404），请检查接口地址"
        return False, f"服务返回 {response.status_code}，请稍后重试或检查配置"

    def verify_api(self, provider):
        """验证并记录结果；catalogue 的 api_ready 取决于 verified。"""
        config = self.api_config(provider)
        if not config:
            raise PipelineError(
                "MODEL_API_NOT_CONFIGURED",
                "该服务尚未配置 API：请先填写密钥或接口地址",
                409,
            )
        spec = next(
            (
                s
                for s in CATALOGUE
                if s.get("kind") == "api" and s.get("api_provider") == provider
            ),
            None,
        )
        if spec is None:
            raise PipelineError("MODEL_API_UNKNOWN", "未知的云 OCR 服务", 422)
        try:
            ok, message = self._probe_api(spec.get("backend"), config)
        except PipelineError:
            raise
        except Exception as error:  # 网络异常不能冒泡为 500
            ok, message = False, f"验证请求失败：{error}"
        apis = self.api_configs()
        entry = dict(apis.get(provider, {}))
        entry["verified"] = ok
        entry["verify_error"] = message
        apis[provider] = entry
        path = self.api_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, apis)
        return ok, message

    @staticmethod
    def package_available(backend):
        import importlib.util

        module = {"paddle": "paddleocr", "easyocr": "easyocr", "doctr": "doctr"}.get(
            backend
        )
        if not module:
            return False
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    def catalogue(self):
        apis = self.api_configs()
        result = []
        for spec in CATALOGUE:
            row = dict(spec)
            kind = spec.get("kind", "local")
            row.setdefault("advanced", False)
            row.setdefault("requires_api", False)
            row["api_ready"] = True
            row["path"] = ""
            if kind == "api":
                config = apis.get(spec.get("api_provider"), {})
                if spec.get("free_key") and not config:
                    # 免费额度通道（如 OCR.space）无需配置即可直接用
                    row["installed"] = True
                    row["api_ready"] = True
                    row["api_verified"] = True
                    row["api_has_key"] = True
                    row["api_error"] = ""
                    result.append(row)
                    continue
                row["installed"] = bool(config)
                row["api_ready"] = bool(config.get("verified"))
                row["api_verified"] = bool(config.get("verified"))
                row["api_error"] = config.get("verify_error") or ""
                row["api_has_key"] = bool(
                    config.get("api_key") or config.get("base_url")
                )
            elif kind == "system":
                row["installed"] = bool(spec.get("supported", False))
            elif kind == "package":
                row["installed"] = self.package_available(spec.get("backend"))
            else:
                try:
                    row["path"] = str(self.folder(spec["id"]))
                    row["installed"] = True
                except PipelineError:
                    row["installed"] = False
            result.append(row)
        return result

    def verify(self, model_id):
        folder = self.folder(model_id)
        manifest = folder / "checksums.json"
        if not manifest.is_file():
            raise PipelineError(
                "MODEL_NOT_VERIFIED", "模型缺少校验清单，请重新导入或下载", 409
            )
        expected = json.loads(manifest.read_text(encoding="utf-8"))
        for name in self.spec(model_id)["files"]:
            with (folder / name).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected.get(name):
                raise PipelineError(
                    "MODEL_CORRUPT", f"模型文件损坏：{model_id}/{name}", 409
                )
        return folder

    def import_folder(self, model_id, source):
        spec = self.spec(model_id)
        source = Path(source).expanduser().resolve()
        candidates = [source, source / "models" / model_id, source / model_id]
        if model_id == "mangaocr":
            candidates.append(source / "mangaocr")
        source = next(
            (p for p in candidates if all((p / n).is_file() for n in spec["files"])),
            None,
        )
        if source is None:
            raise PipelineError(
                "MODEL_FILES_MISSING", "所选目录缺少该模型的权重或配置文件", 422
            )
        self.validate_layout(model_id, source)
        self.root.mkdir(parents=True, exist_ok=True)
        import uuid

        temporary = self.root / ("." + model_id + "-" + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            hashes = {}
            for name in spec["files"]:
                shutil.copyfile(source / name, temporary / name)
                with (temporary / name).open("rb") as stream:
                    hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
            if model_id == LEGACY_BALLOON_ID and any(
                hashes[name] != expected for name, expected in LEGACY_BALLOON_HASHES.items()
            ):
                raise PipelineError(
                    "MODEL_VERSION_CHANGED", "所选权重或 CFG 与 ImageTrans 原版 Legacy 不匹配", 422
                )
            atomic_json(temporary / "checksums.json", hashes)
            target = self.root / model_id
            if target.exists():
                raise PipelineError(
                    "MODEL_EXISTS",
                    "该模型已安装；请先备份并移走旧模型目录再导入新版本",
                    409,
                )
            os.replace(temporary, target)
            return self.catalogue()
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def validate_layout(self, model_id, folder):
        """A generic model.onnx filename is not evidence of model identity."""
        from Backend.infrastructure.manga.engines import legacy_image_params, rfdetr_signature, session

        spec = self.spec(model_id)
        backend = spec.get("backend", "")
        if backend == "yolov4":
            legacy_image_params(folder)
            return
        engine = session(str(folder / spec["files"][0]))
        names = {i.name for i in engine.get_inputs()}
        valid = (
            (backend in {"yolo26", "yolo_generic", "yolo_v8"} and "images" in names)
            or (backend == "mangaocr" and names == {"pixel_values"})
            or (backend in {"rapidocr", "rapidocr_det"} and "x" in names)
            or (
                backend == "lama"
                and len(names) == 2
                and any("mask" in n.lower() for n in names)
            )
            or (backend in {"layout", "rfdetr"} and rfdetr_signature(engine, model_id))
        )
        if not valid:
            raise PipelineError(
                "MODEL_FILES_MISSING", "该文件的模型类型不匹配，请选择对应模型目录", 422
            )

    def download(self, model_id, context=None):
        spec = self.spec(model_id)
        kind = spec.get("kind", "local")
        if kind == "api":
            raise PipelineError(
                "MODEL_API_ONLY",
                "云 API 模型无需下载；请在模型中心点「配置 API」填写密钥",
                409,
            )
        if kind == "system":
            raise PipelineError(
                "MODEL_SYSTEM_OCR", "系统 OCR 无需下载，直接在项目设置中选择使用", 409
            )
        if kind == "package":
            raise PipelineError(
                "MODEL_PACKAGE_REQUIRED",
                "该模型随 pip 包提供：请先安装对应依赖，模型会在首次使用时自动下载",
                409,
            )
        if not spec["url"] or not spec.get("supported", True):
            raise PipelineError(
                "MODEL_IMPORT_REQUIRED", "此模型请从官方来源下载后使用目录导入", 409
            )
        import tempfile

        with tempfile.TemporaryDirectory(prefix="ainiee-manga-") as temp:
            temp = Path(temp)

            def fetch(url, output):
                cache = self.root / ".downloads"
                cache.mkdir(parents=True, exist_ok=True)
                partial = cache / (hashlib.sha256(url.encode()).hexdigest() + ".part")
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with requests.get(
                    url, headers=headers, stream=True, timeout=(15, 60)
                ) as response:
                    if response.status_code == 416:
                        # A complete interrupted download is checked against pinned
                        # contents below, never trusted just because sizes match.
                        shutil.copyfile(partial, output)
                        return
                    response.raise_for_status()
                    resume = response.status_code == 206 and response.headers.get(
                        "Content-Range", ""
                    ).startswith(f"bytes {offset}-")
                    if response.status_code == 206 and not resume:
                        raise PipelineError(
                            "MODEL_DOWNLOAD_RANGE", "下载源返回的续传范围无效", 409
                        )
                    received = offset if resume else 0
                    with partial.open("ab" if resume else "wb") as stream:
                        for chunk in response.iter_content(1024 * 1024):
                            if context and context.is_cancelled():
                                raise PipelineError("CANCELLED", "模型下载已取消", 409)
                            stream.write(chunk)
                            received += len(chunk)
                            if received > 1024**3:
                                raise PipelineError(
                                    "MODEL_TOO_LARGE", "模型下载超过 1GB 限制", 422
                                )
                            if context and context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "model_download",
                                        "model": model_id,
                                        "downloaded_bytes": received,
                                    }
                                )
                shutil.copyfile(partial, output)

            if len(spec["files"]) == 1 and spec["files"][0] == "model.onnx":
                # 单权重模型（如 LaMa）：直接下载
                fetch(spec["url"], temp / "model.onnx")
            elif model_id == "mangaocr":
                for name in spec["files"]:
                    fetch(spec["url"] + name, temp / name)
            else:
                # 检测模型 zip 包：按文件名匹配（包内可能带子目录）
                archive = temp / "model.zip"
                fetch(spec["url"], archive)
                with zipfile.ZipFile(archive) as z:
                    members = {
                        Path(n).name: n
                        for n in z.namelist()
                        if not n.endswith("/")
                    }
                    for name in spec["files"]:
                        member = members.get(name)
                        if member is None:
                            raise PipelineError(
                                "MODEL_VERSION_CHANGED",
                                f"下载包缺少 {name}，请检查官方更新",
                                409,
                            )
                        if z.getinfo(member).file_size > 1024**3:
                            raise PipelineError(
                                "MODEL_TOO_LARGE", "模型文件超过 1GB 限制", 422
                            )
                        with z.open(member) as source, (temp / name).open("wb") as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)
            from Backend.core.paths import backend_resource_path

            lock_path = backend_resource_path("manga", "source-lock.json")
            if lock_path.is_file():
                locked = (
                    json.loads(lock_path.read_text(encoding="utf-8"))
                    .get("files", {})
                    .get(model_id, {})
                )
                for name, expected in locked.items():
                    with (temp / name).open("rb") as stream:
                        actual = hashlib.file_digest(stream, "sha256").hexdigest()
                    if actual != expected:
                        raise PipelineError(
                            "MODEL_VERSION_CHANGED",
                            "下载的模型与已验证版本不符，请检查官方更新后手动导入",
                            409,
                        )
            return self.import_folder(model_id, temp)
