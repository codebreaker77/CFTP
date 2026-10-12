"""CFTP/1.0 Multi-threaded TCP Server Implementation."""

import argparse
import hmac
import json
import logging
import socket
import sys
import threading
from pathlib import Path
from typing import Optional, Tuple, Union

from cftp.config import Config
from cftp.broadcast import BroadcastManager, ClientSession
from cftp.integrity import StreamingHasher, is_valid_sha256
from cftp.protocol import (
    STATUS_ACCESS_DENIED,
    STATUS_BAD_REQUEST,
    STATUS_CONFLICT,
    STATUS_INTERNAL_ERROR,
    STATUS_NOT_FOUND,
    STATUS_NOT_IMPLEMENTED,
    STATUS_OK,
    STATUS_PAYLOAD_TOO_LARGE,
    STATUS_VERSION_NOT_SUPPORTED,
    ConnectionClosedError,
    FramedSocket,
    FramingError,
    ProtocolError,
    Request,
    Response,
)
from cftp.storage import StorageManager, StorageSecurityError

logger = logging.getLogger("cftp.server")


class CFTPServer:
    """Multi-threaded TCP server implementing the CFTP/1.0 specification."""

    def __init__(
        self,
        host: str = Config.DEFAULT_HOST,
        port: int = Config.DEFAULT_PORT,
        storage_dir: Union[str, Path] = Config.DEFAULT_SERVER_STORAGE,
        max_file_size: int = Config.MAX_FILE_SIZE,
        timeout: float = Config.DEFAULT_SOCKET_TIMEOUT,
        max_clients: int = 64,
    ) -> None:
        if max_clients < 1:
            raise ValueError("max_clients must be positive")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_clients = max_clients
        self.storage = StorageManager(storage_dir, max_file_size=max_file_size)
        self._server_sock: Optional[socket.socket] = None
        self._is_running = False
        self._client_threads: list[threading.Thread] = []
        self._client_sockets: set[socket.socket] = set()
        self._sessions = {}
        self._lock = threading.Lock()
        self.broadcasts = BroadcastManager(self)

    def start(self) -> None:
        """Bind TCP socket and begin accepting client connections."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self._server_sock.bind((self.host, self.port))
        except OSError as err:
            logger.error(f"Failed to bind to {self.host}:{self.port}: {err}")
            self._server_sock.close()
            raise

        # Query assigned port if ephemeral (port 0)
        actual_port = self._server_sock.getsockname()[1]
        self.port = actual_port

        self._server_sock.listen(Config.DEFAULT_BACKLOG)
        self._server_sock.settimeout(0.5)
        listener = self._server_sock
        self._is_running = True
        logger.info(
            f"CFTP/1.0 Server listening on {self.host}:{self.port} (Storage: {self.storage.root_dir})"
        )

        try:
            while self._is_running:
                try:
                    client_sock, client_addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if not self._is_running:
                        break
                    raise

                logger.info(f"Accepted TCP connection from {client_addr[0]}:{client_addr[1]}")
                thread = threading.Thread(
                    target=self._handle_client_wrapper,
                    args=(client_sock, client_addr),
                    daemon=True,
                    name=f"Client-{client_addr[0]}:{client_addr[1]}",
                )
                with self._lock:
                    if not self._is_running or len(self._client_sockets) >= self.max_clients:
                        client_sock.close()
                        continue
                    self._client_sockets.add(client_sock)
                    self._client_threads.append(thread)
                    thread.start()
        finally:
            self.stop()

    def stop(self) -> None:
        """Gracefully shut down the server socket and disconnect clients."""
        with self._lock:
            self._is_running = False
            listener = self._server_sock
            self._server_sock = None
            sockets = list(self._client_sockets)
            threads = list(self._client_threads)
        if listener:
            listener.close()
        for client_sock in sockets:
            FramedSocket(client_sock).close()
        self.broadcasts.stop()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        logger.info("CFTP Server stopped.")

    def _handle_client_wrapper(self, client_sock: socket.socket, client_addr: Tuple[str, int]) -> None:
        """Wrapper for client handling with proper cleanup."""
        try:
            client_sock.settimeout(self.timeout)
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            framed = FramedSocket(client_sock)
            with self._lock:
                self._sessions[client_sock] = ClientSession(framed, client_addr)
            self._client_session(framed, client_addr)
        except (ConnectionClosedError, FramingError) as err:
            logger.info(f"Client {client_addr} disconnected ({err})")
        except Exception as err:
            logger.error(f"Unhandled error in client session {client_addr}: {err}", exc_info=True)
        finally:
            try:
                client_sock.close()
            except OSError:
                pass
            with self._lock:
                self._client_sockets.discard(client_sock)
                self._sessions.pop(client_sock, None)
                current = threading.current_thread()
                if current in self._client_threads:
                    self._client_threads.remove(current)
            logger.info(f"Connection closed for client {client_addr[0]}:{client_addr[1]}")

    def _client_session(self, framed: FramedSocket, client_addr: Tuple[str, int]) -> None:
        """Handle request loop for an active client connection."""
        while self._is_running:
            try:
                line = framed.read_line()
            except (ConnectionClosedError, FramingError) as err:
                logger.warning(f"Client {client_addr} connection terminated: {err}")
                break

            if line is None:
                # Orderly client disconnect
                logger.info(f"Client {client_addr} disconnected gracefully.")
                break

            logger.debug(f"Received request from {client_addr}: {line}")

            try:
                req = Request.parse(line)
            except ProtocolError as err:
                logger.warning(f"Protocol parsing error from {client_addr}: {err}")
                resp = Response.error(err.code, str(err))
                with self._sessions[framed.sock].send_lock:
                    framed.send_line(resp.to_wire())
                continue

            # Process command
            session = self._sessions.get(framed.sock)
            try:
                with session.send_lock:
                    should_close = self._dispatch_command(req, framed, client_addr)
            except ProtocolError as err:
                with session.send_lock:
                    framed.send_line(Response.error(err.code, str(err)).to_wire())
                should_close = False
            except StorageSecurityError as err:
                with session.send_lock:
                    framed.send_line(Response.error(STATUS_ACCESS_DENIED, str(err)).to_wire())
                should_close = False
            if should_close:
                break

    def _dispatch_command(
        self, req: Request, framed: FramedSocket, client_addr: Tuple[str, int]
    ) -> bool:
        """Dispatch parsed request to command handler. Returns True if connection should terminate."""
        cmd = req.command.upper()

        if cmd == "HELLO":
            session = self._sessions[framed.sock]
            session.name = req.params.get("NAME", "CFTP client")[:80]
            session.receives = req.params.get("RECEIVES", "0") == "1"
            framed.send_line(Response.ok(reason="CONNECTED", params={"CLIENT_ID": session.client_id, "TIMEOUT": str(self.timeout)}).to_wire())
            return False
        elif cmd == "PING":
            framed.send_line(Response.ok(reason="PONG").to_wire())
            return False
        elif cmd == "ACK":
            self.broadcasts.acknowledge(self._sessions[framed.sock], req.params)
            return False
        elif cmd in ("PEERS", "BSTATUS", "BROADCAST"):
            if cmd == "PEERS":
                value = {"clients": self.broadcasts.peers()}
            elif cmd == "BSTATUS":
                value = self.broadcasts.status(req.params.get("ID", ""))
            else:
                if not req.argument:
                    raise ProtocolError("BROADCAST requires a filename")
                value = self.broadcasts.start(req.argument)
            payload = json.dumps(value, separators=(",", ":")).encode(Config.CHARSET)
            framed.send_line(Response.ok(params={"SIZE": str(len(payload))}).to_wire())
            framed.sock.sendall(payload)
            return False
        elif cmd == "LIST":
            self._handle_list(req, framed, client_addr)
            return False
        elif cmd == "GET":
            self._handle_get(req, framed, client_addr)
            return False
        elif cmd == "PUT":
            self._handle_put(req, framed, client_addr)
            return False
        elif cmd == "QUIT":
            self._handle_quit(req, framed, client_addr)
            return True
        else:
            logger.warning(f"Unsupported command '{cmd}' from {client_addr}")
            resp = Response.error(STATUS_NOT_IMPLEMENTED, f"Command '{cmd}' not recognized.")
            framed.send_line(resp.to_wire())
            return False

    def _handle_list(self, req: Request, framed: FramedSocket, client_addr: Tuple[str, int]) -> None:
        """Handle LIST command: sends directory file list with framing."""
        try:
            files = self.storage.list_files()
            payload = json.dumps(files, separators=(",", ":")).encode(Config.CHARSET)
        except Exception as err:
            logger.error(f"Error handling LIST for {client_addr}: {err}")
            framed.send_line(Response.error(STATUS_INTERNAL_ERROR, "Failed to list files").to_wire())
            return
        try:
            resp = Response.ok(
                reason="OK",
                params={
                    "COUNT": str(len(files)),
                    "SIZE": str(len(payload)),
                },
            )
            framed.send_line(resp.to_wire())
            # Stream explicit payload bytes
            if payload:
                framed.sock.sendall(payload)
            logger.info(f"LIST answered for {client_addr} ({len(files)} files, {len(payload)} bytes)")
        except Exception as err:
            logger.error(f"Error handling LIST for {client_addr}: {err}")
            raise FramingError("LIST payload transfer failed") from err

    def _handle_get(self, req: Request, framed: FramedSocket, client_addr: Tuple[str, int]) -> None:
        """Handle GET command: verifies existence and streams file to client."""
        filename = req.argument
        if not filename:
            framed.send_line(
                Response.error(STATUS_BAD_REQUEST, "GET requires a target filename.").to_wire()
            )
            return

        try:
            safe_path = self.storage.resolve_safe_path(filename)
        except StorageSecurityError as err:
            logger.warning(f"Security violation on GET '{filename}' from {client_addr}: {err}")
            framed.send_line(Response.error(STATUS_ACCESS_DENIED, str(err)).to_wire())
            return

        if not safe_path.is_file():
            logger.info(f"File not found on GET '{filename}' from {client_addr}")
            framed.send_line(
                Response.error(STATUS_NOT_FOUND, f"File not found: '{filename}'").to_wire()
            )
            return

        header_sent = False
        try:
            # Use one descriptor so an atomic PUT replacement cannot change
            # the file between hashing and transmission.
            with open(safe_path, "rb") as f:
                file_size, checksum = self.storage.checksum_for_file(safe_path, f)
                resp = Response.ok(params={"SIZE": str(file_size), "CHECKSUM": checksum})
                header_sent = True
                framed.send_line(resp.to_wire())
                framed.stream_file_to_socket(f, file_size)

            logger.info(
                f"GET '{filename}' transferred {file_size} bytes to {client_addr} (SHA-256: {checksum[:8]}...)"
            )
        except Exception as err:
            logger.error(f"Error streaming GET '{filename}' to {client_addr}: {err}")
            if header_sent:
                raise FramingError("GET payload transfer failed") from err
            code = STATUS_NOT_FOUND if isinstance(err, FileNotFoundError) else STATUS_INTERNAL_ERROR
            framed.send_line(Response.error(code, "Failed to read file").to_wire())

    def _handle_put(self, req: Request, framed: FramedSocket, client_addr: Tuple[str, int]) -> None:
        """Handle PUT command: validates size, staging file, verifies SHA-256 and commits."""
        filename = req.argument
        if not filename:
            framed.send_line(
                Response.error(STATUS_BAD_REQUEST, "PUT requires a target filename.").to_wire()
            )
            return

        try:
            safe_path = self.storage.resolve_safe_path(filename)
        except StorageSecurityError as err:
            logger.warning(f"Security violation on PUT '{filename}' from {client_addr}: {err}")
            framed.send_line(Response.error(STATUS_ACCESS_DENIED, str(err)).to_wire())
            return

        # Parse declared SIZE and CHECKSUM
        size_str = req.params.get("SIZE")
        expected_checksum = req.params.get("CHECKSUM")

        if not size_str or not size_str.isascii() or not size_str.isdigit():
            framed.send_line(
                Response.error(STATUS_BAD_REQUEST, "PUT requires valid positive numeric SIZE parameter.").to_wire()
            )
            return

        file_size = int(size_str)
        if file_size < 0:
            framed.send_line(
                Response.error(STATUS_BAD_REQUEST, "SIZE cannot be negative.").to_wire()
            )
            return

        if file_size > self.storage.max_file_size:
            logger.warning(
                f"Rejected upload of '{filename}' ({file_size} bytes exceeds limit {self.storage.max_file_size})"
            )
            framed.send_line(
                Response.error(
                    STATUS_PAYLOAD_TOO_LARGE,
                    f"File size {file_size} exceeds server maximum limit of {self.storage.max_file_size} bytes.",
                ).to_wire()
            )
            return

        if not is_valid_sha256(expected_checksum):
            framed.send_line(
                Response.error(STATUS_BAD_REQUEST, "PUT requires a valid 64-character SHA-256 CHECKSUM.").to_wire()
            )
            return

        # Acknowledge readiness to receive payload
        ready_resp = Response.ok(reason="READY_FOR_DATA")
        framed.send_line(ready_resp.to_wire())

        # Receive payload into temporary staging file
        temp_path = self.storage.get_temp_path(filename)
        hasher = StreamingHasher()

        try:
            with open(temp_path, "wb") as f:
                framed.stream_exact_to_file(f, file_size, hasher=hasher)

            actual_checksum = hasher.hexdigest()
            if not hmac.compare_digest(actual_checksum, expected_checksum.lower()):
                logger.warning(
                    f"Integrity check failed for PUT '{filename}' from {client_addr}: "
                    f"declared={expected_checksum}, calculated={actual_checksum}"
                )
                self.storage.cleanup_temp_file(temp_path)
                framed.send_line(
                    Response.error(
                        STATUS_CONFLICT,
                        f"Checksum mismatch: expected {expected_checksum}, computed {actual_checksum}",
                        reason="INTEGRITY_CHECK_FAILED",
                    ).to_wire()
                )
                return

            # Atomic commit to final destination
            self.storage.commit_temp_file(temp_path, filename, checksum=actual_checksum)
            logger.info(
                f"PUT '{filename}' successful ({file_size} bytes, SHA-256: {actual_checksum[:8]}...)"
            )
            framed.send_line(
                Response.ok(
                    reason="TRANSFER_COMPLETE",
                    params={"CHECKSUM": actual_checksum, "SIZE": str(file_size)},
                ).to_wire()
            )
        except Exception as err:
            logger.error(f"Error during PUT '{filename}' payload transfer: {err}")
            self.storage.cleanup_temp_file(temp_path)
            raise

    def _handle_quit(self, req: Request, framed: FramedSocket, client_addr: Tuple[str, int]) -> None:
        """Handle QUIT command: acknowledge goodbye and signal session termination."""
        framed.send_line(Response.ok(reason="GOODBYE").to_wire())
        logger.info(f"Client {client_addr} issued QUIT. Closing connection.")


def main() -> None:
    """Command-line entry point for running the CFTP server."""
    parser = argparse.ArgumentParser(description="CFTP/1.0 Custom File Transfer Protocol Server")
    parser.add_argument("--host", default=Config.DEFAULT_HOST, help="Host/IP to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT, help="Port to bind (default: 9000)")
    parser.add_argument("--max-clients", type=int, default=64, help="Maximum concurrent clients (default: 64)")
    parser.add_argument(
        "--storage-dir",
        default=str(Config.DEFAULT_SERVER_STORAGE),
        help="Directory to store files (default: server_storage)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=Config.MAX_FILE_SIZE,
        help="Maximum allowed file size in bytes (default: 100MB)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = CFTPServer(
        host=args.host,
        port=args.port,
        storage_dir=args.storage_dir,
        max_file_size=args.max_size,
        max_clients=args.max_clients,
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down server...")
        server.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
