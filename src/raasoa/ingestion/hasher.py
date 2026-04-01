import hashlib


def content_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def file_hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
