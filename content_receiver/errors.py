"""异常定义。"""


class ContentReceiverError(Exception):
    """本库所有异常的基类。"""


class ManifestConflictError(ContentReceiverError):
    """同一版本号被登记了不同的清单。"""


class UnknownVersionError(ContentReceiverError):
    """投递分片时版本尚未登记。"""


class UnknownFileError(ContentReceiverError):
    """分片指向的文件不在清单中。"""


class InvalidChunkError(ContentReceiverError):
    """分片序号越界，或长度/摘要与清单不符。"""


class InvalidManifestError(ContentReceiverError):
    """清单结构不合法（长度与分片数不一致、路径非法等）。"""
