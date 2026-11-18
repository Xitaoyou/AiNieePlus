"""Transport-independent failures for the explicit-model pipeline."""
from typing import Any


def _restore_pipeline_error(code, message, status_code, details):
    """模块级函数：pickle 只按名字序列化，不能用绑定方法或 lambda。"""
    return PipelineError(code, message, status_code, details)


class PipelineError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def __reduce__(self):
        # 模型推理跑在 spawn 子进程里，异常要靠 pickle 送回来。
        # 默认的 BaseException 重建路径会用 self.args 调用 __init__，
        # 而这里 __init__ 需要 4 个位置参数，args 只有 1 个 message，
        # 重建会抛 TypeError；feeder 线程只打日志不回调 future，
        # 于是父进程的 future 永不完成，HTTP 请求和后续任务全部卡死。
        return (
            _restore_pipeline_error,
            (self.code, self.message, self.status_code, self.details),
        )

    def to_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result
