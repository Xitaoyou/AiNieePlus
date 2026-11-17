"""Editable manga documents: source, erasure, and lettering geometry are independent."""

from typing import Literal
from uuid import uuid4
import math
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def identifier():
    return uuid4().hex


class MangaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Rect(MangaModel):
    x: float = Field(default=0, ge=0)
    y: float = Field(default=0, ge=0)
    width: float = Field(default=100, gt=0)
    height: float = Field(default=60, gt=0)
    rotation: float = Field(default=0, ge=-180, le=180)

    @field_validator("*")
    @classmethod
    def finite(cls, value):
        if not math.isfinite(value):
            raise ValueError("坐标必须是有限数值")
        return value


class LetterStyle(MangaModel):
    font: str = ""
    size: int = Field(default=28, ge=6, le=512)
    min_size: int = Field(default=10, ge=6, le=512)
    autofit: bool = True
    vertical: bool = False
    bold: bool = False
    color: str = "#000000"
    stroke_color: str = "#ffffff"
    stroke_width: int = Field(default=0, ge=0, le=30)
    line_spacing: float = Field(default=1.2, ge=0.5, le=4)
    char_spacing: int = Field(default=0, ge=-10, le=50)
    padding: int = Field(default=4, ge=0, le=100)
    align: Literal["left", "center", "right"] = "center"

    @field_validator("color", "stroke_color")
    @classmethod
    def valid_color(cls, value):
        import re

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("颜色必须为 #RRGGBB")
        return value


class Stroke(MangaModel):
    points: list[tuple[float, float]] = Field(min_length=1, max_length=10000)
    radius: int = Field(default=8, ge=1, le=256)
    erase: bool = False

    @field_validator("points")
    @classmethod
    def finite_points(cls, value):
        if any(not math.isfinite(n) or n < 0 for p in value for n in p):
            raise ValueError("画笔坐标无效")
        return value


class Region(MangaModel):
    id: str = Field(default_factory=identifier, pattern=r"^[a-f0-9]{32}$")
    ocr_rect: Rect = Field(default_factory=Rect)
    erase_rect: Rect = Field(default_factory=Rect)
    layout_rect: Rect = Field(default_factory=Rect)
    source: str = ""
    target: str = ""
    erase_only: bool = False
    translation_state: Literal[
        "idle", "pending", "succeeded", "failed", "cancelled"
    ] = "idle"
    translation_error: str = ""
    source_locked: bool = False
    target_locked: bool = False
    geometry_locked: bool = False
    order: int = Field(default=0, ge=0)
    panel_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    style: LetterStyle = Field(default_factory=LetterStyle)
    removal: Literal["precise", "fill", "none"] = "fill"
    fill_color: str = "auto"
    fill_opacity: float = Field(default=1, ge=0, le=1)
    mask_strokes: list[Stroke] = Field(default_factory=list, max_length=10000)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("fill_color")
    @classmethod
    def valid_fill(cls, value):
        return value if value == "auto" else LetterStyle.valid_color(value)


class MangaPage(MangaModel):
    id: str = Field(default_factory=identifier, pattern=r"^[a-f0-9]{32}$")
    name: str
    source: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    regions: list[Region] = Field(default_factory=list)
    panels: list[dict] = Field(default_factory=list)
    stages: dict[str, dict] = Field(default_factory=dict)


class MangaSettings(MangaModel):
    detector: str = "manga-panel"
    ocr: str = "mangaocr"
    source_language: str = "ja"
    target_language: str = "zh-CN"
    provider_id: str | None = None
    right_to_left: bool = True
    detect_panels: bool = True
    confidence: float = Field(default=0.5, ge=0.05, le=1)
    removal: Literal["precise", "fill", "none"] = "fill"
    auto_recognize: bool = True


class MangaWorkspace(MangaModel):
    schema_version: Literal[1] = 1
    kind: Literal["manga"] = "manga"
    id: str = Field(default_factory=identifier, pattern=r"^[a-f0-9]{32}$")
    title: str = "漫画工程"
    revision: int = Field(default=0, ge=0)
    pages: list[MangaPage] = Field(default_factory=list)
    settings: MangaSettings = Field(default_factory=MangaSettings)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [p.id for p in self.pages] + [r.id for p in self.pages for r in p.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("页面和文字框 ID 重复")
        for page in self.pages:
            for region in page.regions:
                for rect in (region.ocr_rect, region.erase_rect, region.layout_rect):
                    if (
                        rect.x + rect.width > page.width + 0.01
                        or rect.y + rect.height > page.height + 0.01
                    ):
                        raise ValueError("文字框超出图片边界")
        return self
