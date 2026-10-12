"""Configuration constants and configuration classes for CFTP."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Protocol and application runtime configuration defaults."""

    # Protocol metadata
    PROTOCOL_VERSION: str = "CFTP/1.0"
    LINE_DELIMITER: bytes = b"\r\n"
    CHARSET: str = "utf-8"

    # Network defaults
    DEFAULT_HOST: str = "127.0.0.1"
    DEFAULT_PORT: int = 9000
    DEFAULT_BACKLOG: int = 10
    DEFAULT_SOCKET_TIMEOUT: float = 30.0

    # Framing limits & buffers
    BUFFER_SIZE: int = 65536  # 64 KB bounded payload I/O chunks
    MAX_CONTROL_LINE_LENGTH: int = 4096  # Max length in bytes for command / response line

    # Transfer & security limits
    MAX_FILE_SIZE: int = 100 * 1024 * 1024  # 100 MB default max file size limit

    # Storage paths
    DEFAULT_SERVER_STORAGE: Path = Path("server_storage")
    DEFAULT_CLIENT_DOWNLOADS: Path = Path("downloads")
    DEFAULT_CLIENT_UPLOADS: Path = Path("uploads")
