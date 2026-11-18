import threading

from Backend.domain.models.cache_item import TranslationStatus
from Backend.domain.models.cache_project import CacheProject, ProjectType


class CodeTextFilter:
    """Magika 依赖 onnxruntime，延迟到真正需要识别文本类型时再加载。

    这样翻译流水线在 ORT 不可用的机器上也能运行，只跳过代码文本检测。
    """

    SUPPORT_PROJECT_TYPES = frozenset([ProjectType.MTOOL])
    _magika = None
    _lock = threading.Lock()

    @classmethod
    def engine(cls):
        if cls._magika is None:
            with cls._lock:
                if cls._magika is None:
                    from magika import Magika

                    cls._magika = Magika()
        return cls._magika

    def get_label(self, text: str):
        from magika.types.content_type_label import ContentTypeLabel

        try:
            result = self.engine().identify_bytes(text.encode())
        except Exception:
            # 模型不可用时按普通文本处理，避免整条流水线中断
            return ContentTypeLabel.TXT
        return result.output.label

    def filter_text(self, event_data: CacheProject):
        for item in event_data.items_iter(self.SUPPORT_PROJECT_TYPES):
            if item.translation_status != TranslationStatus.UNTRANSLATED:
                continue
            label = self.get_label(item.source_text)
            if label != ContentTypeLabel.TXT:
                item.translation_status = TranslationStatus.QUESTIONABLE
