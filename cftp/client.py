"""CFTP/1.0 Client Implementation with Interactive Shell and CLI."""

import argparse
import hmac
import json
import logging
import os
import socket
import select
import re
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from cftp.config import Config
from cftp.integrity import (
    StreamingHasher,
    calculate_file_sha256,
    is_valid_sha256,
)
from cftp.protocol import (
    STATUS_OK,
    ConnectionClosedError,
    FramedSocket,
    FramingError,
    ProtocolError,
    Request,
    Response,
)
from cftp.storage import StorageManager, StorageSecurityError

logger = logging.getLogger("cftp.client")


def serialized(method):
    """Serialize request streams and defer event ACKs until request payloads finish."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._io_lock:
            try:
                return method(self, *args, **kwargs)
            except (FramingError, ConnectionError):
                self.disconnect()
                raise
            finally:
                self._flush_acknowledgments()
    return call


@dataclass
class TransferResult:
    """Detailed summary of a completed or failed file transfer."""

    success: bool
    filename: str
    bytes_transferred: int
    duration_seconds: float
    throughput_mb_s: float
    expected_sha256: str
    actual_sha256: str
    integrity_matched: bool
    error_message: Optional[str] = None

    def print_summary(self) -> None:
        """Print an academic transfer summary report to stdout."""
        status_str = "SUCCESS" if self.success else "FAILED"
        integrity_str = "MATCHED (VERIFIED)" if self.integrity_matched else "MISMATCH / UNVERIFIED"
        print("+" + "-" * 62 + "+")
        print(f"| TRANSFER RESULT: {status_str:<43} |")
        print("+" + "-" * 62 + "+")
        print(f"| File:               {self.filename:<40} |")
        print(f"| Transferred:        {self.bytes_transferred} bytes ({self.bytes_transferred / (1024*1024):.2f} MB)")
        print(f"| Duration:           {self.duration_seconds:.4f} seconds")
        print(f"| Throughput:         {self.throughput_mb_s:.2f} MB/s")
        print(f"| Expected SHA-256:   {self.expected_sha256[:16]}...")
        print(f"| Actual SHA-256:     {self.actual_sha256[:16]}...")
        print(f"| Integrity Status:   {integrity_str}")
        if self.error_message:
            print(f"| Error:              {self.error_message}")
        print("+" + "-" * 62 + "+")


class CFTPClient:
    """Client for CFTP/1.0 custom file transfer protocol."""

    # Windows cannot reliably replace one destination from two threads at once.
    # Downloads still stream concurrently; only the final rename is serialized.
    _download_commit_lock = threading.Lock()

    def __init__(
        self,
        host: str = Config.DEFAULT_HOST,
        port: int = Config.DEFAULT_PORT,
        download_dir: Union[str, Path] = Config.DEFAULT_CLIENT_DOWNLOADS,
        timeout: float = Config.DEFAULT_SOCKET_TIMEOUT,
        receive_broadcasts: bool = True,
        client_name: Optional[str] = None,
        broadcast_subdirectories: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.download_dir = Path(download_dir).absolute().resolve()
        self.timeout = timeout
        self.receive_broadcasts = receive_broadcasts
        self.client_name = client_name or socket.gethostname()
        self.broadcast_subdirectories = broadcast_subdirectories
        self.client_id = None
        self._io_lock = threading.RLock()
        self._receiver_stop = threading.Event()
        self._receiver_thread = None
        self._pending_acks = []
        self.received_broadcasts = []
        self.on_broadcast = None
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._sock: Optional[socket.socket] = None
        self._framed: Optional[FramedSocket] = None

    @property
    def is_connected(self) -> bool:
        """Check if client currently maintains an open TCP socket."""
        return self._framed is not None

    @serialized
    def connect(self) -> None:
        """Establish TCP connection to the CFTP server."""
        if self.is_connected:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.connect((self.host, self.port))
        except OSError as err:
            sock.close()
            raise ConnectionError(f"Failed to connect to CFTP server at {self.host}:{self.port}: {err}") from err

        self._sock = sock
        self._framed = FramedSocket(sock)
        self._framed.send_line(Request(command="HELLO", params={"NAME": self.client_name,
            "RECEIVES": "1" if self.receive_broadcasts else "0"}).to_wire())
        line = self._framed.read_line()
        if line is None:
            raise ConnectionClosedError("Server closed during registration")
        response = Response.parse(line)
        if response.code not in (STATUS_OK, 501):
            self.disconnect()
            raise ProtocolError(f"Server rejected connection: {response.code}")
        self.client_id = response.params.get("CLIENT_ID")
        if self.receive_broadcasts and response.code == STATUS_OK:
            self._receiver_stop = threading.Event()
            heartbeat = max(0.1, min(10.0, float(response.params.get("TIMEOUT", "30")) / 3))
            self._receiver_thread = threading.Thread(target=self._receive_events,
                args=(self._framed, self._receiver_stop, heartbeat), daemon=True,
                name=f"CFTP-Receiver-{self.client_id}")
            self._receiver_thread.start()
        logger.info(f"Connected to CFTP server at {self.host}:{self.port}")

    @serialized
    def disconnect(self) -> None:
        """Close the connection gracefully."""
        self._receiver_stop.set()
        self._pending_acks.clear()
        if self._framed:
            try:
                self._framed.close()
            except Exception:
                pass
            self._framed = None
            self._sock = None
        logger.info("Disconnected from CFTP server.")

    @serialized
    def list_files(self) -> List[Dict[str, Union[str, int]]]:
        """Request remote file listing via LIST command."""
        self._ensure_connected()
        req = Request(command="LIST")
        self._framed.send_line(req.to_wire())

        resp_line = self._read_response_line()
        if resp_line is None:
            raise ConnectionClosedError("Server closed connection unexpectedly.")

        resp = Response.parse(resp_line)
        if resp.code != STATUS_OK:
            raise ProtocolError(f"Server error on LIST: {resp.code} {resp.reason} ({resp.params.get('MESSAGE', '')})")

        payload_size = int(resp.params.get("SIZE", "0"))
        payload_bytes = self._framed.read_exact(payload_size)

        if not payload_bytes:
            return []

        try:
            files = json.loads(payload_bytes.decode(Config.CHARSET))
            return files
        except Exception as err:
            raise ProtocolError(f"Failed to parse server LIST payload: {err}") from err

    @serialized
    def get_file(
        self,
        remote_filename: str,
        local_destination: Optional[Union[str, Path]] = None,
        progress_callback: Optional[callable] = None,
    ) -> TransferResult:
        """Download a file from server with SHA-256 integrity verification.

        Safely streams into a temporary .cftp_part file, checks hash against
        server's CHECKSUM header, and atomically replaces local target on success.
        """
        self._ensure_connected()
        safe_name = StorageManager.validate_filename(remote_filename)

        if local_destination is None:
            local_path = (self.download_dir / safe_name).resolve()
        else:
            local_path = Path(local_destination).resolve()

        req = Request(command="GET", argument=safe_name)
        start_time = time.perf_counter()
        self._framed.send_line(req.to_wire())

        resp_line = self._read_response_line()
        if resp_line is None:
            raise ConnectionClosedError("Server closed connection during GET.")

        resp = Response.parse(resp_line)
        if resp.code != STATUS_OK:
            msg = resp.params.get("MESSAGE", resp.reason)
            return TransferResult(
                success=False,
                filename=safe_name,
                bytes_transferred=0,
                duration_seconds=time.perf_counter() - start_time,
                throughput_mb_s=0.0,
                expected_sha256="",
                actual_sha256="",
                integrity_matched=False,
                error_message=f"Server returned {resp.code} {resp.reason}: {msg}",
            )

        try:
            expected_size = int(resp.params["SIZE"])
            if expected_size < 0:
                raise ValueError("Negative payload size")
        except (KeyError, ValueError) as err:
            self.disconnect()
            raise ProtocolError("GET response requires a nonnegative SIZE") from err
        expected_checksum = resp.params.get("CHECKSUM", "")
        if not is_valid_sha256(expected_checksum):
            self.disconnect()
            raise ProtocolError("GET response requires a valid SHA-256 CHECKSUM")

        # Stream into temporary staging file
        temp_dest = None
        hasher = StreamingHasher()

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=local_path.parent,
                prefix=f"{local_path.name}.", suffix=".cftp_part", delete=False,
            ) as f:
                temp_dest = Path(f.name)
                self._framed.stream_exact_to_file(
                    f, expected_size, hasher=hasher, progress_cb=progress_callback
                )

            duration = max(time.perf_counter() - start_time, 1e-9)
            actual_checksum = hasher.hexdigest()
            integrity_match = hmac.compare_digest(actual_checksum, expected_checksum.lower())

            if not integrity_match:
                if temp_dest.exists():
                    temp_dest.unlink()
                return TransferResult(
                    success=False,
                    filename=safe_name,
                    bytes_transferred=expected_size,
                    duration_seconds=duration,
                    throughput_mb_s=(expected_size / (1024 * 1024)) / duration,
                    expected_sha256=expected_checksum,
                    actual_sha256=actual_checksum,
                    integrity_matched=False,
                    error_message=f"SHA-256 mismatch: expected {expected_checksum}, calculated {actual_checksum}",
                )

            # Atomic commit to destination
            with self._download_commit_lock:
                os.replace(temp_dest, local_path)
            return TransferResult(
                success=True,
                filename=safe_name,
                bytes_transferred=expected_size,
                duration_seconds=duration,
                throughput_mb_s=(expected_size / (1024 * 1024)) / duration,
                expected_sha256=expected_checksum,
                actual_sha256=actual_checksum,
                integrity_matched=True,
            )
        except Exception:
            self.disconnect()
            if temp_dest is not None and temp_dest.exists():
                try:
                    temp_dest.unlink()
                except OSError:
                    pass
            raise

    @serialized
    def put_file(
        self,
        local_path: Union[str, Path],
        remote_filename: Optional[str] = None,
        progress_callback: Optional[callable] = None,
    ) -> TransferResult:
        """Upload a file to the server with SHA-256 checksum negotiation."""
        self._ensure_connected()
        src_path = Path(local_path).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"Local file not found: {src_path}")

        target_name = remote_filename or src_path.name
        safe_name = StorageManager.validate_filename(target_name)

        start_time = time.perf_counter()
        file_size = src_path.stat().st_size
        sha256_hash = calculate_file_sha256(src_path)

        # Send PUT request header
        req = Request(
            command="PUT",
            argument=safe_name,
            params={"SIZE": str(file_size), "CHECKSUM": sha256_hash},
        )
        self._framed.send_line(req.to_wire())

        # Wait for server readiness response
        resp_line = self._read_response_line()
        if resp_line is None:
            raise ConnectionClosedError("Server closed connection before acknowledging PUT.")

        resp = Response.parse(resp_line)
        if resp.code != STATUS_OK or resp.reason != "READY_FOR_DATA":
            msg = resp.params.get("MESSAGE", resp.reason)
            return TransferResult(
                success=False,
                filename=safe_name,
                bytes_transferred=0,
                duration_seconds=time.perf_counter() - start_time,
                throughput_mb_s=0.0,
                expected_sha256=sha256_hash,
                actual_sha256="",
                integrity_matched=False,
                error_message=f"Server rejected PUT: {resp.code} {resp.reason} ({msg})",
            )

        # Stream payload to server
        try:
            with open(src_path, "rb") as f:
                self._framed.stream_file_to_socket(
                    f, file_size, hasher=None, progress_cb=progress_callback
                )

            # Await completion response
            final_line = self._read_response_line()
            if final_line is None:
                raise ConnectionClosedError("Server closed connection before confirming transfer.")
            final_resp = Response.parse(final_line)
        except Exception:
            self.disconnect()
            raise
        duration = max(time.perf_counter() - start_time, 1e-9)

        if final_resp.code == STATUS_OK:
            server_checksum = final_resp.params.get("CHECKSUM", "")
            integrity_match = (
                is_valid_sha256(server_checksum)
                and hmac.compare_digest(server_checksum.lower(), sha256_hash)
            )
            return TransferResult(
                success=integrity_match,
                filename=safe_name,
                bytes_transferred=file_size,
                duration_seconds=duration,
                throughput_mb_s=(file_size / (1024 * 1024)) / duration,
                expected_sha256=sha256_hash,
                actual_sha256=server_checksum,
                integrity_matched=integrity_match,
                error_message=None if integrity_match else "Upload completion checksum is missing, invalid, or mismatched",
            )
        else:
            msg = final_resp.params.get("MESSAGE", final_resp.reason)
            return TransferResult(
                success=False,
                filename=safe_name,
                bytes_transferred=file_size,
                duration_seconds=duration,
                throughput_mb_s=(file_size / (1024 * 1024)) / duration,
                expected_sha256=sha256_hash,
                actual_sha256=final_resp.params.get("CHECKSUM", ""),
                integrity_matched=False,
                error_message=f"Server error {final_resp.code} {final_resp.reason}: {msg}",
            )

    @serialized
    def quit(self) -> None:
        """Send QUIT request and close connection."""
        if not self.is_connected:
            return

        try:
            req = Request(command="QUIT")
            self._framed.send_line(req.to_wire())
            resp_line = self._read_response_line()
            if resp_line:
                resp = Response.parse(resp_line)
                logger.info(f"Server acknowledged QUIT: {resp.reason}")
        except Exception as err:
            logger.warning(f"Error during QUIT handshake: {err}")
        finally:
            self.disconnect()

    def _ensure_connected(self) -> None:
        """Ensure connection is open or reconnect."""
        if not self.is_connected:
            self.connect()

    def _flush_acknowledgments(self):
        while self._pending_acks and self._framed is not None:
            params = self._pending_acks.pop(0)
            try:
                self._framed.send_line(Request(command="ACK", params=params).to_wire())
            except OSError:
                self.disconnect()
                break

    def _read_response_line(self):
        while True:
            line = self._framed.read_line()
            if line is None:
                return None
            response = Response.parse(line)
            if response.code != 210:
                return line
            self._receive_broadcast(response)

    def _receive_broadcast(self, response):
        filename = StorageManager.validate_filename(response.params.get("FILENAME", ""))
        checksum = response.params.get("CHECKSUM", "")
        try:
            size = int(response.params["SIZE"])
        except (KeyError, ValueError) as err:
            raise FramingError("Invalid broadcast size") from err
        if not 0 <= size <= Config.MAX_FILE_SIZE or not is_valid_sha256(checksum):
            raise FramingError("Invalid broadcast metadata")
        broadcast_id = response.params.get("ID", "")
        if re.fullmatch(r"[0-9a-f]{32}", broadcast_id) is None:
            raise FramingError("Invalid broadcast ID")
        directory = self.download_dir / broadcast_id if self.broadcast_subdirectories else self.download_dir
        # Resolve through the storage fence, including existing symlinks.
        destination = StorageManager(directory).resolve_safe_path(filename)
        started = time.perf_counter()
        hasher = StreamingHasher()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=directory,
                    prefix="broadcast-", suffix=".cftp_part", delete=False) as target:
                temp_path = Path(target.name)
                self._framed.stream_exact_to_file(target, size, hasher=hasher)
            matched = hmac.compare_digest(checksum.lower(), hasher.hexdigest())
            if matched:
                with self._download_commit_lock:
                    os.replace(temp_path, destination)
            duration = max(time.perf_counter() - started, 1e-9)
            result = TransferResult(matched, filename, size, duration, size / (1024 * 1024) / duration,
                checksum, hasher.hexdigest(), matched, None if matched else "Broadcast checksum mismatch")
            entry = {**asdict(result), "id": response.params.get("ID", ""),
                "direction": "broadcast", "completed_at": datetime.now(timezone.utc).isoformat(),
                "path": str(destination) if matched else None}
            self.received_broadcasts.insert(0, entry)
            del self.received_broadcasts[100:]
            self._pending_acks.append({"ID": entry["id"], "CHECKSUM": hasher.hexdigest(),
                "STATUS": "VERIFIED" if matched else "FAILED"})
            if self.on_broadcast:
                try:
                    self.on_broadcast(entry)
                except Exception:
                    logger.exception("Broadcast callback failed")
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def _receive_events(self, framed, stop, heartbeat):
        next_ping = time.monotonic() + heartbeat
        while not stop.is_set():
            try:
                ready, _, _ = select.select([framed.sock], [], [], min(0.2, heartbeat))
                with self._io_lock:
                    if stop.is_set() or self._framed is not framed:
                        break
                    # A foreground request may have consumed data while this
                    # thread waited for the lock. Recheck readiness under it.
                    ready = bool(select.select([framed.sock], [], [], 0)[0])
                    if ready or framed.buffered_bytes_count:
                        line = framed.read_line()
                        if line is None:
                            raise ConnectionClosedError("Broadcast connection closed")
                        response = Response.parse(line)
                        if response.code != 210:
                            raise FramingError("Unexpected unsolicited response")
                        self._receive_broadcast(response)
                        self._flush_acknowledgments()
                    if time.monotonic() >= next_ping:
                        framed.send_line(Request(command="PING").to_wire())
                        if self._read_response_line() is None:
                            raise ConnectionClosedError("Heartbeat connection closed")
                        self._flush_acknowledgments()
                        next_ping = time.monotonic() + heartbeat
            except (OSError, ValueError, FramingError, ProtocolError, StorageSecurityError):
                with self._io_lock:
                    if self._framed is framed:
                        self.disconnect()
                break

    @serialized
    def _json_command(self, command, argument=None, params=None):
        self._ensure_connected()
        self._framed.send_line(Request(command=command, argument=argument, params=params or {}).to_wire())
        line = self._read_response_line()
        if line is None:
            raise ConnectionClosedError("Server closed during command")
        response = Response.parse(line)
        if response.code != STATUS_OK:
            raise ProtocolError(response.params.get("MESSAGE", response.reason), code=response.code)
        size = int(response.params.get("SIZE", "0"))
        if not 0 <= size <= 1024 * 1024:
            raise FramingError("Invalid command response size")
        return json.loads(self._framed.read_exact(size))

    def connected_clients(self):
        return self._json_command("PEERS")["clients"]

    def broadcast_file(self, filename):
        return self._json_command("BROADCAST", argument=StorageManager.validate_filename(filename))

    def broadcast_status(self, broadcast_id):
        return self._json_command("BSTATUS", params={"ID": broadcast_id})


def run_interactive_repl(client: CFTPClient) -> None:
    """Run interactive CFTP terminal session."""
    print("=" * 64)
    print("  CFTP/1.0 — Custom File Transfer Protocol Interactive Shell")
    print(f"  Target Server: {client.host}:{client.port}")
    print("  Type 'help' for available commands, 'quit' to exit.")
    print("=" * 64)

    try:
        client.connect()
        print(f"[+] Connected to {client.host}:{client.port}\n")
    except Exception as err:
        print(f"[-] Connection failed: {err}")
        return

    while True:
        try:
            line = input("cftp> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        if not line:
            continue

        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            client.quit()
            print("Session terminated. Goodbye!")
            break
        elif cmd == "help":
            print("\nCFTP Commands:")
            print("  list                       - List files hosted on the server")
            print("  get <remote_file> [local]  - Download file with SHA-256 verification")
            print("  put <local_file> [remote]  - Upload file with SHA-256 verification")
            print("  status                     - Show connection status and settings")
            print("  quit                       - Terminate TCP session and exit\n")
        elif cmd == "status":
            conn_str = "CONNECTED" if client.is_connected else "DISCONNECTED"
            print(f"Status: {conn_str}")
            print(f"Server: {client.host}:{client.port}")
            print(f"Downloads directory: {client.download_dir}\n")
        elif cmd == "list":
            try:
                files = client.list_files()
                if not files:
                    print("(No files on server)")
                else:
                    print(f"\n{'Filename':<30} {'Size (Bytes)':<14} {'Modified (UTC)':<24}")
                    print("-" * 70)
                    for item in files:
                        print(f"{item['name']:<30} {item['size']:<14} {item['modified'][:19]:<24}")
                    print()
            except Exception as err:
                print(f"[-] Error listing files: {err}\n")
        elif cmd == "get":
            if len(parts) < 2:
                print("Usage: get <remote_filename> [optional_local_destination]\n")
                continue
            remote_name = parts[1]
            local_dest = parts[2] if len(parts) > 2 else None
            try:
                print(f"[*] Downloading '{remote_name}'...")
                res = client.get_file(remote_name, local_destination=local_dest)
                res.print_summary()
            except Exception as err:
                print(f"[-] GET failed: {err}\n")
        elif cmd == "put":
            if len(parts) < 2:
                print("Usage: put <local_filepath> [optional_remote_name]\n")
                continue
            local_file = parts[1]
            remote_name = parts[2] if len(parts) > 2 else None
            try:
                print(f"[*] Uploading '{local_file}'...")
                res = client.put_file(local_file, remote_filename=remote_name)
                res.print_summary()
            except Exception as err:
                print(f"[-] PUT failed: {err}\n")
        else:
            print(f"Unknown command: '{cmd}'. Type 'help' for command list.\n")


def main() -> None:
    """Entry point for CFTP client."""
    parser = argparse.ArgumentParser(description="CFTP/1.0 Custom File Transfer Protocol Client")
    parser.add_argument("--host", default=Config.DEFAULT_HOST, help="Server IP/host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT, help="Server port (default: 9000)")
    parser.add_argument(
        "--download-dir",
        default=str(Config.DEFAULT_CLIENT_DOWNLOADS),
        help="Local directory for downloaded files (default: downloads)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # CLI sub-commands
    subparsers.add_parser("list", help="List server files")
    subparsers.add_parser("listen", help="Stay connected and receive broadcasts automatically")
    subparsers.add_parser("peers", help="List connected clients")
    broadcast_parser = subparsers.add_parser("broadcast", help="Broadcast a server file to all connected clients")
    broadcast_parser.add_argument("filename")

    get_parser = subparsers.add_parser("get", help="Download a file")
    get_parser.add_argument("filename", help="Remote filename to download")
    get_parser.add_argument("--dest", default=None, help="Local destination file or directory")

    put_parser = subparsers.add_parser("put", help="Upload a file")
    put_parser.add_argument("filepath", help="Path to local file to upload")
    put_parser.add_argument("--remote-name", default=None, help="Name for file on server")

    args = parser.parse_args()

    client = CFTPClient(host=args.host, port=args.port, download_dir=args.download_dir)

    if not args.command:
        # Launch interactive REPL
        run_interactive_repl(client)
        return

    # Non-interactive CLI mode
    try:
        client.connect()
        if args.command == "listen":
            client.on_broadcast = lambda entry: print(
                f"Broadcast {'verified' if entry['success'] else 'FAILED'}: {entry['filename']} -> {entry.get('path')}", flush=True)
            print(f"Listening as {client.client_id}. Files arrive in {client.download_dir}. Ctrl+C to stop.", flush=True)
            while client.is_connected:
                time.sleep(0.2)
        elif args.command == "peers":
            print(json.dumps(client.connected_clients(), indent=2))
        elif args.command == "broadcast":
            job = client.broadcast_file(args.filename)
            deadline = time.monotonic() + 60
            while job["status"] == "sending" and time.monotonic() < deadline:
                time.sleep(0.1)
                job = client.broadcast_status(job["id"])
            print(json.dumps(job, indent=2))
            if job["status"] != "completed":
                sys.exit(1)
        elif args.command == "list":
            files = client.list_files()
            if not files:
                print("(No files on server)")
            else:
                print(f"{'Filename':<30} {'Size (Bytes)':<14} {'Modified (UTC)':<24}")
                print("-" * 70)
                for item in files:
                    print(f"{item['name']:<30} {item['size']:<14} {item['modified'][:19]:<24}")
        elif args.command == "get":
            res = client.get_file(args.filename, local_destination=args.dest)
            res.print_summary()
            if not res.success:
                sys.exit(1)
        elif args.command == "put":
            res = client.put_file(args.filepath, remote_filename=args.remote_name)
            res.print_summary()
            if not res.success:
                sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.quit()


if __name__ == "__main__":
    main()
