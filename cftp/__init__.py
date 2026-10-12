"""CFTP — Custom Application Layer File Transfer Protocol over TCP.

An academic networking protocol implementation demonstrating application-layer
protocol design, message framing over TCP byte streams, end-to-end SHA-256
integrity verification, and safe file transfers.
"""

__version__ = "1.0.0"
__protocol_version__ = "CFTP/1.0"

from cftp.config import Config
from cftp.protocol import Request, Response, FramedSocket, ProtocolError, FramingError
from cftp.integrity import calculate_file_sha256, verify_file_sha256, StreamingHasher
from cftp.storage import StorageManager


def __getattr__(name):
    """Keep CLI modules lazy so ``python -m`` executes each module only once."""
    from importlib import import_module
    modules = {"CFTPServer": "cftp.server", "CFTPClient": "cftp.client"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(modules[name]), name)
    globals()[name] = value
    return value

__all__ = [
    "Config",
    "Request",
    "Response",
    "FramedSocket",
    "ProtocolError",
    "FramingError",
    "calculate_file_sha256",
    "verify_file_sha256",
    "StreamingHasher",
    "StorageManager",
    "CFTPServer",
    "CFTPClient",
]
