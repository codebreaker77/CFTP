"""File integrity verification utilities using SHA-256 for CFTP."""

import hashlib
import hmac
import re
from pathlib import Path
from typing import Union

from cftp.config import Config


def is_valid_sha256(value: str) -> bool:
    """Return whether a value is a 64-character hexadecimal SHA-256 digest."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


class StreamingHasher:
    """Incremental SHA-256 hasher tracking both digest and byte count.

    Enables bounded-memory streaming checksum computation during TCP socket I/O.
    """

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        self._bytes_processed: int = 0

    def update(self, chunk: bytes) -> None:
        """Update hasher with a chunk of bytes."""
        self._hasher.update(chunk)
        self._bytes_processed += len(chunk)

    @property
    def bytes_processed(self) -> int:
        """Total number of bytes processed so far."""
        return self._bytes_processed

    def hexdigest(self) -> str:
        """Return the lowercase hexadecimal SHA-256 digest."""
        return self._hasher.hexdigest().lower()


def calculate_file_sha256(filepath: Union[str, Path], chunk_size: int = Config.BUFFER_SIZE) -> str:
    """Calculate the SHA-256 hexadecimal digest of a file in bounded memory chunks.

    Args:
        filepath: Path to the target file.
        chunk_size: Number of bytes to read per iteration (default 64KB).

    Returns:
        Lowercase hexadecimal SHA-256 digest string.

    Raises:
        FileNotFoundError: If the target file does not exist.
        PermissionError: If reading is denied.
    """
    path = Path(filepath)
    hasher = StreamingHasher()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def calculate_bytes_sha256(data: bytes) -> str:
    """Calculate the SHA-256 hexadecimal digest of an in-memory byte sequence."""
    return hashlib.sha256(data).hexdigest().lower()


def verify_file_sha256(filepath: Union[str, Path], expected_hash: str) -> bool:
    """Verify whether a file's calculated SHA-256 digest matches the expected hash.

    Uses hmac.compare_digest for constant-time comparison to prevent timing attacks.

    Args:
        filepath: Path to the file to verify.
        expected_hash: Hexadecimal expected SHA-256 digest string.

    Returns:
        True if the digests match, False otherwise.
    """
    actual_hash = calculate_file_sha256(filepath)
    return hmac.compare_digest(actual_hash.lower(), expected_hash.lower().strip())
