"""CFTP/1.0 Protocol specifications, request/response models, and TCP framing."""

import shlex
import os
import socket
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple, Union

from cftp.config import Config


class ProtocolError(Exception):
    """Raised when a request or response violates CFTP syntax or semantics."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


class FramingError(Exception):
    """Raised when TCP stream framing, delimiter, or payload boundary fails."""


class ConnectionClosedError(FramingError):
    """Raised when peer closes TCP connection unexpectedly."""


# Standard CFTP status codes
STATUS_OK = 200
STATUS_BAD_REQUEST = 400
STATUS_ACCESS_DENIED = 403
STATUS_NOT_FOUND = 404
STATUS_CONFLICT = 409
STATUS_PAYLOAD_TOO_LARGE = 413
STATUS_INTERNAL_ERROR = 500
STATUS_NOT_IMPLEMENTED = 501
STATUS_VERSION_NOT_SUPPORTED = 505

REASON_PHRASES: Dict[int, str] = {
    STATUS_OK: "OK",
    STATUS_BAD_REQUEST: "BAD_REQUEST",
    STATUS_ACCESS_DENIED: "ACCESS_DENIED",
    STATUS_NOT_FOUND: "NOT_FOUND",
    STATUS_CONFLICT: "CONFLICT",
    STATUS_PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    STATUS_INTERNAL_ERROR: "INTERNAL_SERVER_ERROR",
    STATUS_NOT_IMPLEMENTED: "NOT_IMPLEMENTED",
    STATUS_VERSION_NOT_SUPPORTED: "VERSION_NOT_SUPPORTED",
}


@dataclass
class Request:
    """Represents a structured CFTP client request."""

    command: str
    version: str = Config.PROTOCOL_VERSION
    argument: Optional[str] = None
    params: Dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> str:
        """Serialize request to protocol control line string."""
        parts = [self.version, self.command.upper()]
        if self.argument is not None:
            # Quote argument if it contains spaces or quotes
            if " " in self.argument or '"' in self.argument or "'" in self.argument:
                escaped = self.argument.replace('"', '\\"')
                parts.append(f'"{escaped}"')
            else:
                parts.append(self.argument)

        for k, v in self.params.items():
            if " " in v or '"' in v:
                escaped_v = v.replace('"', '\\"')
                parts.append(f'{k}="{escaped_v}"')
            else:
                parts.append(f"{k}={v}")

        return " ".join(parts)

    @classmethod
    def parse(cls, line: str) -> "Request":
        """Parse raw control line into structured Request object.

        Raises:
            ProtocolError: If line syntax is malformed.
        """
        line = line.strip()
        if not line:
            raise ProtocolError("Empty request line.", code=STATUS_BAD_REQUEST)

        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as err:
            raise ProtocolError(f"Malformed request quoting: {err}", code=STATUS_BAD_REQUEST) from err

        if len(tokens) < 2:
            raise ProtocolError("Request must contain at least version and command.", code=STATUS_BAD_REQUEST)

        version = tokens[0].strip()
        command = tokens[1].strip().upper()

        if version != Config.PROTOCOL_VERSION:
            raise ProtocolError(f"Unsupported protocol version: '{version}'", code=STATUS_VERSION_NOT_SUPPORTED)

        argument: Optional[str] = None
        params: Dict[str, str] = {}

        for token in tokens[2:]:
            token = token.strip()
            # Strip surrounding quotes from argument if present
            if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                token = token[1:-1].replace('\\"', '"')
            elif token.startswith("'") and token.endswith("'") and len(token) >= 2:
                token = token[1:-1]

            if argument is None and command in ("GET", "PUT", "BROADCAST"):
                argument = token
            elif "=" in token:
                key, val = token.split("=", 1)
                key = key.strip().upper()
                val = val.strip()
                if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                    val = val[1:-1].replace('\\"', '"')
                params[key] = val
            elif argument is None:
                argument = token
            else:
                raise ProtocolError(f"Unexpected token in request: '{token}'", code=STATUS_BAD_REQUEST)

        return cls(command=command, version=version, argument=argument, params=params)


@dataclass
class Response:
    """Represents a structured CFTP server response."""

    code: int
    reason: str
    version: str = Config.PROTOCOL_VERSION
    params: Dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> str:
        """Serialize response to protocol control line string."""
        parts = [self.version, str(self.code), self.reason]
        for k, v in self.params.items():
            if " " in v or '"' in v:
                escaped_v = v.replace('"', '\\"')
                parts.append(f'{k}="{escaped_v}"')
            else:
                parts.append(f"{k}={v}")
        return " ".join(parts)

    @classmethod
    def ok(cls, reason: str = "OK", params: Optional[Dict[str, str]] = None) -> "Response":
        """Factory for 200 OK responses."""
        return cls(code=STATUS_OK, reason=reason, params=params or {})

    @classmethod
    def error(cls, code: int, message: str, reason: Optional[str] = None) -> "Response":
        """Factory for error responses."""
        phrase = reason or REASON_PHRASES.get(code, "ERROR")
        return cls(code=code, reason=phrase, params={"MESSAGE": message})

    @classmethod
    def parse(cls, line: str) -> "Response":
        """Parse raw control line into structured Response object.

        Raises:
            ProtocolError: If line syntax is malformed.
        """
        line = line.strip()
        if not line:
            raise ProtocolError("Empty response line from server.", code=STATUS_BAD_REQUEST)

        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as err:
            raise ProtocolError(f"Malformed response quoting: {err}", code=STATUS_BAD_REQUEST) from err

        if len(tokens) < 3:
            raise ProtocolError("Response must contain version, code, and reason phrase.", code=STATUS_BAD_REQUEST)

        version = tokens[0].strip()
        if version != Config.PROTOCOL_VERSION:
            raise ProtocolError(f"Unsupported protocol version: '{version}'", code=STATUS_VERSION_NOT_SUPPORTED)

        try:
            code = int(tokens[1])
        except ValueError as err:
            raise ProtocolError(f"Invalid status code: '{tokens[1]}'", code=STATUS_BAD_REQUEST) from err

        reason = tokens[2].strip()
        params: Dict[str, str] = {}

        for token in tokens[3:]:
            token = token.strip()
            if "=" in token:
                key, val = token.split("=", 1)
                key = key.strip().upper()
                val = val.strip()
                if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                    val = val[1:-1].replace('\\"', '"')
                params[key] = val
            else:
                # Flag parameter
                params[token.upper()] = "TRUE"

        return cls(code=code, reason=reason, version=version, params=params)


class FramedSocket:
    """Manages buffered streaming I/O over a TCP byte-stream socket.

    Ensures correct delimiter-based control framing and exact byte-count payload framing,
    preserving unread bytes across transitions and never discarding adjacent data.
    """

    def __init__(
        self,
        sock: socket.socket,
        buffer_size: int = Config.BUFFER_SIZE,
        max_line_length: int = Config.MAX_CONTROL_LINE_LENGTH,
    ) -> None:
        self.sock = sock
        self.buffer_size = buffer_size
        self.max_line_length = max_line_length
        self._buffer = bytearray()

    @property
    def buffered_bytes_count(self) -> int:
        """Return the number of unconsumed bytes currently residing in the receive buffer."""
        return len(self._buffer)

    def read_line(self) -> Optional[str]:
        """Read a single newline-delimited UTF-8 control line.

        Returns:
            The decoded line without trailing delimiter, or None if connection closed cleanly at EOF.

        Raises:
            FramingError: If max_line_length exceeded or EOF occurs before line delimiter.
        """
        while True:
            # Check for newline in currently buffered bytes
            newline_idx = self._buffer.find(b"\n")
            if newline_idx != -1:
                line_length = newline_idx - (1 if self._buffer[newline_idx - 1:newline_idx] == b"\r" else 0)
                if line_length > self.max_line_length:
                    raise FramingError("Control message exceeded maximum line limit.")
                # Delimiter found: extract line
                raw_line = bytes(self._buffer[:newline_idx])
                # Remove extracted line and '\n' from internal buffer
                del self._buffer[: newline_idx + 1]

                # Strip trailing '\r' if present
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]

                try:
                    return raw_line.decode(Config.CHARSET)
                except UnicodeDecodeError as err:
                    raise FramingError(f"Control line contains invalid UTF-8 bytes: {err}") from err

            # Check if buffer has grown beyond maximum allowed control message size
            if len(self._buffer) > self.max_line_length + (1 if self._buffer.endswith(b"\r") else 0):
                raise FramingError(
                    f"Control message exceeded maximum line limit of {self.max_line_length} bytes."
                )

            # Receive more bytes from the TCP socket
            try:
                chunk = self.sock.recv(min(self.buffer_size, 4096))
            except (socket.timeout, TimeoutError) as err:
                raise FramingError("Socket timed out waiting for control line.") from err
            except OSError as err:
                raise ConnectionClosedError(f"Socket error while reading line: {err}") from err

            if not chunk:
                # Socket EOF reached
                if self._buffer:
                    raise FramingError("Connection closed before complete line delimiter was received.")
                return None  # Clean EOF with empty buffer

            self._buffer.extend(chunk)

    def send_line(self, line: str) -> None:
        """Send a control line terminated with CFTP line delimiter (\\r\\n)."""
        wire_data = line.strip("\r\n").encode(Config.CHARSET) + Config.LINE_DELIMITER
        self.sock.sendall(wire_data)

    def read_exact(self, n_bytes: int) -> bytes:
        """Read exactly n_bytes from the TCP stream.

        Args:
            n_bytes: Exact byte count to read.

        Returns:
            bytes object of length n_bytes.

        Raises:
            FramingError: If socket closes before n_bytes are accumulated.
        """
        if n_bytes < 0:
            raise FramingError(f"Requested byte count cannot be negative: {n_bytes}")
        if n_bytes == 0:
            return b""

        while len(self._buffer) < n_bytes:
            needed = n_bytes - len(self._buffer)
            to_read = min(self.buffer_size, max(needed, 1024))
            try:
                chunk = self.sock.recv(to_read)
            except (socket.timeout, TimeoutError) as err:
                raise FramingError(f"Socket timed out while waiting for {needed} payload bytes.") from err
            except OSError as err:
                raise ConnectionClosedError(f"Socket read error: {err}") from err

            if not chunk:
                raise ConnectionClosedError(
                    f"Unexpected EOF: expected {n_bytes} bytes, received {len(self._buffer)} bytes."
                )
            self._buffer.extend(chunk)

        result = bytes(self._buffer[:n_bytes])
        del self._buffer[:n_bytes]
        return result

    def stream_exact_to_file(
        self,
        dest_file,
        n_bytes: int,
        hasher=None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Stream exactly n_bytes from socket directly into an open binary file.

        Uses bounded memory chunks (O(1) memory) regardless of file size.
        Maintains SHA-256 hasher state simultaneously.

        Args:
            dest_file: Open binary file-like object in write mode.
            n_bytes: Total number of bytes to transfer.
            hasher: Optional StreamingHasher instance to update on the fly.
            progress_cb: Optional callback func(bytes_written_so_far, total_bytes).

        Returns:
            Total bytes written (equals n_bytes).
        """
        if n_bytes < 0:
            raise FramingError(f"Negative byte count requested: {n_bytes}")

        bytes_written = 0

        # Phase 1: Drain any bytes already accumulated in buffer up to n_bytes
        if self._buffer:
            from_buffer = min(len(self._buffer), n_bytes)
            chunk = bytes(self._buffer[:from_buffer])
            del self._buffer[:from_buffer]

            dest_file.write(chunk)
            if hasher:
                hasher.update(chunk)
            bytes_written += len(chunk)
            if progress_cb:
                progress_cb(bytes_written, n_bytes)

        # Phase 2: Read remaining payload directly from socket
        while bytes_written < n_bytes:
            remaining = n_bytes - bytes_written
            to_read = min(self.buffer_size, remaining)
            try:
                chunk = self.sock.recv(to_read)
            except (socket.timeout, TimeoutError) as err:
                raise FramingError(
                    f"Socket timed out waiting for payload ({bytes_written}/{n_bytes} bytes read)."
                ) from err
            except OSError as err:
                raise ConnectionClosedError(
                    f"Socket error during payload transfer ({bytes_written}/{n_bytes} read): {err}"
                ) from err

            if not chunk:
                raise ConnectionClosedError(
                    f"Premature EOF: expected {n_bytes} bytes, got only {bytes_written} bytes."
                )

            dest_file.write(chunk)
            if hasher:
                hasher.update(chunk)
            bytes_written += len(chunk)
            if progress_cb:
                progress_cb(bytes_written, n_bytes)

        return bytes_written

    def stream_file_to_socket(
        self,
        src_file,
        n_bytes: int,
        hasher=None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Stream exactly n_bytes from an open binary file to the TCP socket.

        Args:
            src_file: Open binary file-like object in read mode.
            n_bytes: Total number of bytes to send.
            hasher: Optional StreamingHasher instance to update on the fly.
            progress_cb: Optional callback func(bytes_sent_so_far, total_bytes).

        Returns:
            Total bytes sent (equals n_bytes).
        """
        if n_bytes < 0:
            raise FramingError(f"Negative byte count requested: {n_bytes}")

        bytes_sent = 0
        # Python uses OS sendfile where supported and a bounded fallback on
        # Windows. Hashing transfers continue through the regular chunk loop.
        if n_bytes and hasher is None and progress_cb is None and hasattr(os, "sendfile"):
            try:
                src_file.fileno()
            except (AttributeError, OSError, ValueError):
                pass
            else:
                try:
                    count = self.sock.sendfile(src_file, offset=src_file.tell(), count=n_bytes)
                except OSError as err:
                    raise ConnectionClosedError(f"Socket sendfile failed: {err}") from err
                if count != n_bytes:
                    raise FramingError(f"Source file ended prematurely: expected {n_bytes}, sent {count}")
                return count
        while bytes_sent < n_bytes:
            remaining = n_bytes - bytes_sent
            to_read = min(self.buffer_size, remaining)
            chunk = src_file.read(to_read)
            if not chunk:
                raise FramingError(
                    f"Source file ended prematurely: expected {n_bytes} bytes, read {bytes_sent} bytes."
                )

            try:
                self.sock.sendall(chunk)
            except OSError as err:
                raise ConnectionClosedError(f"Socket write failed during transfer: {err}") from err

            if hasher:
                hasher.update(chunk)
            bytes_sent += len(chunk)
            if progress_cb:
                progress_cb(bytes_sent, n_bytes)

        return bytes_sent

    def close(self) -> None:
        """Close the underlying TCP socket safely."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            self.sock.close()
