from raasoa.ingestion.hasher import content_hash, file_hash


def test_content_hash_deterministic() -> None:
    h1 = content_hash("Hello World")
    h2 = content_hash("Hello World")
    assert h1 == h2


def test_content_hash_different_for_different_content() -> None:
    h1 = content_hash("Hello")
    h2 = content_hash("World")
    assert h1 != h2


def test_file_hash_deterministic() -> None:
    h1 = file_hash(b"test data")
    h2 = file_hash(b"test data")
    assert h1 == h2


def test_hash_is_sha256() -> None:
    h = content_hash("test")
    assert len(h) == 32  # SHA-256 = 32 bytes
