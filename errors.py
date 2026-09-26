"""跨层共用的错误类型。"""
from __future__ import annotations


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request", details: object = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        # details 用于携带结构化信息（例如执行前核对出的阻塞项清单）
        self.details = details
