"""Local browser interface backed by real CFTP TCP transfers.

Run ``python -m cftp.web --demo`` to start both the TCP server and web UI.
No third-party runtime dependencies are required.
"""

import argparse
import json
import logging
import mimetypes
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from cftp.client import CFTPClient
from cftp.config import Config
from cftp.protocol import FramingError, ProtocolError
from cftp.server import CFTPServer
from cftp.storage import StorageManager, StorageSecurityError


ASSETS = Path(__file__).resolve().parent / "static"


class WebError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class WebState:
    """Connection preferences and recent transfers for this local UI instance."""

    def __init__(self, host="127.0.0.1", port=9000):
        self.host = host
        self.port = port
        self.connected = False
        self.activity = []
        self.inbox = []
        self.broadcast_jobs = {}
        self._client = None
        self._directory = tempfile.TemporaryDirectory(prefix="cftp-web-inbox-")
        self.lock = threading.RLock()

    def status(self):
        with self.lock:
            return {"host": self.host, "port": self.port, "connected": self.connected and (self._client is None or self._client.is_connected),
                    "max_file_size": Config.MAX_FILE_SIZE, "version": Config.PROTOCOL_VERSION}

    @contextmanager
    def client(self, directory=None):
        with self.lock:
            if not self.connected:
                raise WebError("Connect to a server first.", 409)
            if self._client is None:
                self._client = self.make_client(self.host, self.port)
            client = self._client
        try:
            client.connect()
            yield client
        except (ConnectionError, FramingError, OSError):
            with self.lock:
                if client is self._client:
                    self.connected = False
            raise

    def make_client(self, host, port):
        client = CFTPClient(host=host, port=port, download_dir=self._directory.name,
            timeout=30, client_name="Web workspace", broadcast_subdirectories=True)
        client.on_broadcast = self.record_broadcast
        return client

    def record_broadcast(self, entry):
        with self.lock:
            self.inbox.insert(0, dict(entry))
            self.activity.insert(0, dict(entry))
            del self.inbox[100:]
            del self.activity[100:]

    def disconnect(self):
        with self.lock:
            client, self._client = self._client, None
            self.connected = False
        if client:
            client.quit()

    def close(self):
        self.disconnect()
        self._directory.cleanup()

    def record(self, direction, result):
        entry = {**asdict(result), "id": uuid.uuid4().hex, "direction": direction,
                 "completed_at": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            self.activity.insert(0, entry)
            del self.activity[100:]
        return entry


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state=None):
        self.state = state or WebState()
        super().__init__(address, WebHandler)

    def server_close(self):
        self.state.close()
        super().server_close()


class WebHandler(BaseHTTPRequestHandler):
    server_version = "CFTP-Web/1.0"

    def log_message(self, message, *args):
        logging.getLogger("cftp.web").debug(message, *args)

    def _headers(self, status, content_type, size=None, extra=None):
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if size is not None:
            self.send_header("Content-Length", str(size))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, value, status=200):
        payload = json.dumps(value).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _body_size(self, maximum):
        try:
            size = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            raise WebError("A valid Content-Length is required.", 411)
        if size < 0:
            raise WebError("Payload size cannot be negative.")
        if size > maximum:
            raise WebError("This file exceeds the 100 MB transfer limit.", 413)
        return size

    def _read_json(self):
        size = self._body_size(4096)
        try:
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise WebError("Invalid JSON request.")
        if not isinstance(payload, dict):
            raise WebError("Expected a JSON object.")
        return payload

    def _guard(self, mutation=False):
        # Local API is deliberately same-origin, including uploads.
        authority = self.headers.get("Host", "")
        parsed = urlsplit("http://" + authority)
        try:
            valid = parsed.hostname in ("localhost", "127.0.0.1", "::1") and parsed.port == self.server.server_port
        except ValueError:
            valid = False
        if not valid:
            raise WebError("Use this interface through its local URL.", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + authority:
            raise WebError("Cross-origin requests are not allowed.", 403)
        if mutation and self.headers.get("X-CFTP-Request") != "1":
            raise WebError("Missing local request header.", 403)

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, mutation):
        self.response_started = False
        self.connection.settimeout(30)
        try:
            self._guard(mutation)
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            if mutation:
                self._post(target.path, query)
            else:
                self._get(target.path, query)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except WebError as err:
            self._error(str(err), err.status)
        except StorageSecurityError as err:
            self._error(str(err), 400)
        except (ConnectionError, FramingError, socket.timeout):
            self._error("The CFTP server is unavailable. Check the address and reconnect.", 502)
        except ProtocolError as err:
            self._error(str(err), err.code if err.code in (400, 403, 404, 409, 413) else 502)
        except Exception:
            logging.getLogger("cftp.web").exception("Web request failed")
            self._error("The operation failed. Please retry.", 500)

    def _error(self, message, status):
        if self.response_started:
            self.close_connection = True
            return
        self._json({"error": message}, status)

    def _get(self, path, query):
        state = self.server.state
        if path == "/api/status":
            self._json(state.status())
        elif path == "/api/activity":
            with state.lock:
                entries = list(state.activity)
            self._json({"transfers": entries})
        elif path == "/api/broadcasts":
            clients = []
            with state.lock:
                jobs = list(state.broadcast_jobs.values())
            if state.status()["connected"]:
                with state.client() as client:
                    clients = client.connected_clients()
                    for index, job in enumerate(jobs):
                        if job["status"] == "sending":
                            try:
                                jobs[index] = client.broadcast_status(job["id"])
                            except ProtocolError:
                                jobs[index] = {**job, "status": "failed"}
                with state.lock:
                    for job in jobs:
                        state.broadcast_jobs[job["id"]] = job
            with state.lock:
                inbox = [{key: value for key, value in entry.items() if key != "path"} for entry in state.inbox]
            self._json({"clients": clients, "jobs": jobs[::-1], "inbox": inbox, "connection": state.status()})
        elif path == "/api/inbox-download":
            broadcast_id = query.get("id", [""])[0]
            with state.lock:
                entry = next((dict(item) for item in state.inbox if item["id"] == broadcast_id), None)
            if not entry or not entry["success"]:
                raise WebError("Received broadcast not found.", 404)
            source_path = Path(entry["path"]).resolve()
            source_path.relative_to(Path(state._directory.name).resolve())
            self._headers(200, "application/octet-stream", entry["bytes_transferred"], {
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(entry["filename"], safe=""),
                "X-CFTP-Checksum": entry["actual_sha256"],
            })
            with source_path.open("rb") as source:
                while chunk := source.read(Config.BUFFER_SIZE):
                    self.wfile.write(chunk)
        elif path == "/api/files":
            with tempfile.TemporaryDirectory(prefix="cftp-web-") as directory, state.client(directory) as client:
                files = client.list_files()
            self._json({"files": files})
        elif path == "/api/download":
            filename = StorageManager.validate_filename(query.get("filename", [""])[0])
            with tempfile.TemporaryDirectory(prefix="cftp-web-") as directory, state.client(directory) as client:
                destination = Path(directory) / filename
                result = client.get_file(filename, local_destination=destination)
                entry = state.record("download", result)
                if not result.success:
                    raise WebError(result.error_message or "Download failed.", 404 if "404" in (result.error_message or "") else 409)
                self._headers(200, "application/octet-stream", result.bytes_transferred, {
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                    "X-CFTP-Checksum": result.actual_sha256,
                    "X-CFTP-Transfer": entry["id"],
                })
                with destination.open("rb") as source:
                    while chunk := source.read(Config.BUFFER_SIZE):
                        self.wfile.write(chunk)
        else:
            assets = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/atlas.css": "atlas.css", "/favicon.svg": "favicon.svg"}
            name = assets.get(path)
            if not name:
                raise WebError("Not found.", 404)
            payload = (ASSETS / name).read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._headers(200, mime + ("; charset=utf-8" if mime.startswith("text/") else ""), len(payload))
            self.wfile.write(payload)

    def _post(self, path, query):
        state = self.server.state
        if path == "/api/connect":
            payload = self._read_json()
            host = payload.get("host", "127.0.0.1")
            port = payload.get("port", 9000)
            if not isinstance(host, str) or not host.strip() or len(host) > 253:
                raise WebError("Enter a valid server address.")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise WebError("Port must be between 1 and 65535.")
            client = state.make_client(host.strip(), port)
            client.timeout = 5
            try:
                client.connect()
                files = client.list_files()
                client._sock.settimeout(30)
                client.timeout = 30
            except Exception:
                client.quit()
                raise
            with state.lock:
                old_client = state._client
                state._client = client
                state.host, state.port, state.connected = host.strip(), port, True
            if old_client:
                old_client.quit()
            self._json({**state.status(), "files": files})
        elif path == "/api/disconnect":
            state.disconnect()
            self._json(state.status())
        elif path == "/api/broadcast":
            filename = StorageManager.validate_filename(self._read_json().get("filename", ""))
            with state.client() as client:
                job = client.broadcast_file(filename)
            with state.lock:
                state.broadcast_jobs[job["id"]] = job
            self._json(job, 202)
        elif path == "/api/upload":
            filename = StorageManager.validate_filename(query.get("filename", [""])[0])
            size = self._body_size(Config.MAX_FILE_SIZE)
            if not state.status()["connected"]:
                raise WebError("Connect to a server first.", 409)
            with tempfile.TemporaryDirectory(prefix="cftp-web-") as directory:
                source = Path(directory) / filename
                remaining = size
                with source.open("wb") as output:
                    while remaining:
                        chunk = self.rfile.read(min(Config.BUFFER_SIZE, remaining))
                        if not chunk:
                            raise WebError("Upload was interrupted.")
                        output.write(chunk)
                        remaining -= len(chunk)
                # Connect after receiving the browser's body so a slow HTTP
                # upload does not consume the TCP session's idle timeout.
                with state.client(directory) as client:
                    result = client.put_file(source)
                entry = state.record("upload", result)
            self._json(entry, 200 if result.success else 409)
        else:
            raise WebError("Not found.", 404)


def main():
    parser = argparse.ArgumentParser(description="CFTP local web interface")
    parser.add_argument("--port", type=int, default=8787, help="Local web port (default: 8787)")
    parser.add_argument("--cftp-host", default="127.0.0.1")
    parser.add_argument("--cftp-port", type=int, default=9000)
    parser.add_argument("--demo", action="store_true", help="Also start a local CFTP server")
    parser.add_argument("--storage-dir", default="server_storage")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    tcp_server = None
    worker = None
    web = None
    try:
        if args.demo:
            tcp_server = CFTPServer(port=args.cftp_port, storage_dir=args.storage_dir)
            worker = threading.Thread(target=tcp_server.start, daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            while not tcp_server._is_running and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not tcp_server._is_running:
                raise RuntimeError("Could not start the CFTP server. Use a different --cftp-port or omit --demo to use an existing server.")
        state = WebState("127.0.0.1" if args.demo else args.cftp_host,
                         tcp_server.port if tcp_server else args.cftp_port)
        if tcp_server:
            state.connected = True
        web = WebServer(("127.0.0.1", args.port), state)
        print(f"CFTP interface: http://127.0.0.1:{web.server_port}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if web:
            web.server_close()
        if tcp_server:
            tcp_server.stop()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
