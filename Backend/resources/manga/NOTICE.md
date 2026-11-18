# Public manga resources

See `bundle.json` for origins and `source-lock.json` for pinned file digests.
Large model/font binaries are prepared by `python -m Tools.prepare_manga_models --download`, not committed to Git.

- manga-panel: author's ONNX conversion of leoxs22/manga-panel-detector-yolo26n, AGPL-3.0.
- mangaOCR: ONNX conversion of kha-white/manga-ocr, upstream Apache-2.0.
- RapidOCR 3.10.0: PP-OCRv6 small detector/recognizer and mobile v2 classifier; Apache-2.0.
- LaMa: mayocream/lama-manga-onnx, Apache-2.0 model card.
- NotoSansCJKjp-Regular.otf: SIL Open Font License 1.1, see fonts/OFL.txt.

Third-party model weights follow their own licences and are not modified.
For original model cards, source projects, current licenses and reproduction links, see docs/manga.md.
