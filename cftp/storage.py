"""Safe filesystem storage abstraction and path validation for CFTP."""

import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from cftp.config import Config
from cftp.integrity import calculate_file_sha256
from cftp.integrity import StreamingHasher

# Reserved device names in Windows filesystems
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# Disallowed characters in cross-platform filenames
INVALID_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class StorageSecurityError(Exception):
    """Raised when an unsafe path or illegal filename is encountered."""


class StorageManager:
    """Confines all filesystem storage operations to a designated root directory.

    Guarantees:
    - Path traversal prevention (e.g. '../', absolute paths).
    - Windows reserved device name neutralization.
    - Controlled atomic writes via temporary files.
    - Thread-safe file metadata queries and operations.
    - Cleanup of partial or abandoned transfers.
    """

    def __init__(self, root_dir: Union[str, Path], max_file_size: int = Config.MAX_FILE_SIZE) -> None:
        self.root_dir = Path(root_dir).absolute().resolve()
        self.max_file_size = max_file_size
        self._lock = threading.RLock()
        self._checksum_cache = OrderedDict()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _fingerprint(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def checksum_for_file(self, path, source):
        """Cache hashes by descriptor metadata; replaced or edited files miss the cache."""
        stat = os.fstat(source.fileno())
        key = (str(path), self._fingerprint(stat))
        with self._lock:
            cached = self._checksum_cache.get(key)
            if cached is not None:
                self._checksum_cache.move_to_end(key)
                return stat.st_size, cached
        source.seek(0)
        hasher = StreamingHasher()
        while chunk := source.read(Config.BUFFER_SIZE):
            hasher.update(chunk)
        source.seek(0)
        if self._fingerprint(stat) != self._fingerprint(os.fstat(source.fileno())):
            raise OSError("File changed while its checksum was being calculated")
        self.remember_checksum(path, stat, hasher.hexdigest())
        return hasher.bytes_processed, hasher.hexdigest()

    def remember_checksum(self, path, stat, checksum):
        with self._lock:
            self._checksum_cache[(str(path), self._fingerprint(stat))] = checksum
            while len(self._checksum_cache) > 512:
                self._checksum_cache.popitem(last=False)

    @staticmethod
    def validate_filename(filename: str) -> str:
        """Validate and sanitize a requested filename.

        Args:
            filename: The relative filename provided by a client or user.

        Returns:
            The validated filename string.

        Raises:
            StorageSecurityError: If the filename violates security policies.
        """
        if not filename or not isinstance(filename, str):
            raise StorageSecurityError("Filename cannot be empty.")

        clean_name = filename.strip()
        if not clean_name:
            raise StorageSecurityError("Filename cannot be blank.")

        # Reject null bytes or control characters
        if "\x00" in clean_name or INVALID_CHARS_PATTERN.search(clean_name):
            raise StorageSecurityError(f"Filename contains illegal characters: '{clean_name}'")

        # Disallow directory separators
        if "/" in clean_name or "\\" in clean_name:
            raise StorageSecurityError(f"Directory separators are not allowed in filename: '{clean_name}'")

        # Disallow directory traversal markers
        if clean_name in (".", "..") or clean_name.startswith("../") or clean_name.startswith("..\\"):
            raise StorageSecurityError(f"Path traversal detected in filename: '{clean_name}'")

        # Check Windows reserved base stems
        if clean_name.endswith("."):
            raise StorageSecurityError("Filenames cannot end with a dot.")
        if clean_name.endswith(".cftp_part"):
            raise StorageSecurityError("The .cftp_part suffix is reserved for temporary transfers.")
        base_stem = clean_name.split(".", 1)[0].rstrip(" ").upper()
        if base_stem in WINDOWS_RESERVED_NAMES:
            raise StorageSecurityError(f"Filename '{clean_name}' conflicts with OS reserved device name '{base_stem}'")

        return clean_name

    def resolve_safe_path(self, filename: str) -> Path:
        """Resolve a filename safely within the root directory.

        Guarantees that the resulting canonical path is strictly contained within
        self.root_dir, preventing path traversal attacks.

        Args:
            filename: Target relative filename.

        Returns:
            Resolved absolute Path object within self.root_dir.

        Raises:
            StorageSecurityError: If traversal escapes the root directory.
        """
        valid_name = self.validate_filename(filename)
        candidate_path = (self.root_dir / valid_name).resolve()

        # Enforce that candidate is strictly inside root_dir
        try:
            candidate_path.relative_to(self.root_dir)
        except ValueError as err:
            raise StorageSecurityError(
                f"Path traversal attempted: '{filename}' escapes root '{self.root_dir}'"
            ) from err

        return candidate_path

    def file_exists(self, filename: str) -> bool:
        """Check if a file exists in the storage directory."""
        path = self.resolve_safe_path(filename)
        return path.is_file()

    def get_file_info(self, filename: str) -> Optional[Dict[str, Union[str, int]]]:
        """Get file metadata including size, modified timestamp, and SHA-256."""
        with self._lock:
            path = self.resolve_safe_path(filename)
            if not path.is_file():
                return None

            with path.open("rb") as source:
                stat = os.fstat(source.fileno())
                _, sha256 = self.checksum_for_file(path, source)
            mod_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            return {
                "name": path.name,
                "size": stat.st_size,
                "sha256": sha256,
                "modified": mod_dt,
            }

    def list_files(self) -> List[Dict[str, Union[str, int]]]:
        """List all regular files stored in the storage directory with metadata."""
        with self._lock:
            results: List[Dict[str, Union[str, int]]] = []
            for entry in sorted(self.root_dir.iterdir()):
                # Ignore temporary partial transfer files and directories
                if entry.is_file() and not entry.is_symlink() and not entry.name.endswith(".cftp_part"):
                    try:
                        with entry.open("rb") as source:
                            stat = os.fstat(source.fileno())
                            _, sha256 = self.checksum_for_file(entry, source)
                        mod_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                        results.append({
                            "name": entry.name,
                            "size": stat.st_size,
                            "sha256": sha256,
                            "modified": mod_dt,
                        })
                    except (OSError, FileNotFoundError):
                        # File may have been removed concurrently
                        continue
            return results

    def get_temp_path(self, filename: str) -> Path:
        """Generate a path for a temporary staging file during upload."""
        safe_path = self.resolve_safe_path(filename)
        temp_name = f"{safe_path.name}.{os.getpid()}_{threading.get_ident()}.cftp_part"
        return self.root_dir / temp_name

    def commit_temp_file(self, temp_path: Path, target_filename: str, checksum=None) -> Path:
        """Atomically rename a verified temporary file to the final target filename."""
        with self._lock:
            target_path = self.resolve_safe_path(target_filename)
            temp_resolved = temp_path.resolve()
            if not temp_resolved.is_file():
                raise FileNotFoundError(f"Staged file '{temp_path}' does not exist.")

            # Atomic replacement
            os.replace(temp_resolved, target_path)
            if checksum is not None:
                self.remember_checksum(target_path, target_path.stat(), checksum)
            return target_path

    def cleanup_temp_file(self, temp_path: Path) -> None:
        """Safely delete an uncommitted temporary file if it exists."""
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

    def delete_file(self, filename: str) -> bool:
        """Delete a file from the managed storage directory."""
        with self._lock:
            path = self.resolve_safe_path(filename)
            if path.is_file():
                path.unlink()
                return True
            return False
