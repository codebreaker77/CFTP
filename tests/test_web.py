"""HTTP-to-CFTP integration tests, using real loopback TCP transfers."""

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

from cftp.config import Config
from cftp.server import CFTPServer
from cftp.web import WebServer, WebState


class TestWebWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.tcp = CFTPServer(port=0, storage_dir=cls.directory.name)
        cls.tcp_thread = threading.Thread(target=cls.tcp.start, daemon=True)
        cls.tcp_thread.start()
        deadline = time.monotonic() + 3
        while not cls.tcp._is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.tcp._is_running:
            raise RuntimeError("Test TCP server failed to start")
        cls.web = WebServer(("127.0.0.1", 0), WebState(port=cls.tcp.port))
        cls.web_thread = threading.Thread(target=cls.web.serve_forever, daemon=True)
        cls.web_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.web.shutdown()
        cls.web.server_close()
        cls.web_thread.join(timeout=3)
        cls.tcp.stop()
        cls.tcp_thread.join(timeout=3)
        cls.directory.cleanup()

    def setUp(self):
        self.web.state.disconnect()
        self.web.state.activity.clear()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)
        request_headers = {"X-CFTP-Request": "1", **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def connect(self):
        status, _, body = self.request("POST", "/api/connect", {"host": "127.0.0.1", "port": self.tcp.port})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["connected"])

    def test_frontend_and_assets_are_served_with_security_headers(self):
        for path in ("/", "/app.js", "/styles.css", "/favicon.svg"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_connect_list_and_disconnect(self):
        self.connect()
        status, _, body = self.request("GET", "/api/files")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body)["files"], list)
        status, _, body = self.request("POST", "/api/disconnect")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["connected"])
        self.assertEqual(self.request("GET", "/api/files")[0], 409)

    def test_binary_upload_download_and_activity(self):
        self.connect()
        content = bytes(range(256)) * 513
        filename = "web transfer=" + uuid.uuid4().hex + ".bin"
        path = quote(filename, safe="")
        status, _, body = self.request("POST", "/api/upload?filename=" + path, content)
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result["success"])
        self.assertTrue(result["integrity_matched"])
        self.assertEqual((Path(self.directory.name) / filename).read_bytes(), content)
        status, headers, body = self.request("GET", "/api/download?filename=" + path)
        self.assertEqual(status, 200)
        self.assertEqual(body, content)
        self.assertEqual(headers["X-CFTP-Checksum"], hashlib.sha256(content).hexdigest())
        self.assertIn("filename*=UTF-8''", headers["Content-Disposition"])
        status, _, body = self.request("GET", "/api/activity")
        activity = json.loads(body)["transfers"]
        self.assertEqual([item["direction"] for item in activity], ["download", "upload"])

    def test_empty_file_round_trip(self):
        self.connect()
        filename = "empty-" + uuid.uuid4().hex + ".txt"
        self.assertEqual(self.request("POST", "/api/upload?filename=" + filename, b"")[0], 200)
        status, headers, body = self.request("GET", "/api/download?filename=" + filename)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["X-CFTP-Checksum"], hashlib.sha256(b"").hexdigest())

    def test_upload_replaces_existing_file_after_verification(self):
        self.connect()
        filename = "replace-" + uuid.uuid4().hex + ".txt"
        for content in (b"original", b"replacement"):
            self.assertEqual(self.request("POST", "/api/upload?filename=" + filename, content)[0], 200)
        self.assertEqual((Path(self.directory.name) / filename).read_bytes(), b"replacement")

    def test_missing_download_returns_json_error(self):
        self.connect()
        status, headers, body = self.request("GET", "/api/download?filename=missing.txt")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("404", json.loads(body)["error"])

    def test_upload_rejects_path_traversal(self):
        self.connect()
        status, _, body = self.request("POST", "/api/upload?filename=..%2Foutside.txt", b"unsafe")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_upload_size_limit_is_enforced_before_reading_body(self):
        self.connect()
        status, _, _ = self.request("POST", "/api/upload?filename=large.txt", b"",
                                    {"Content-Length": str(Config.MAX_FILE_SIZE + 1)})
        self.assertEqual(status, 413)

    def test_mutations_require_local_request_header(self):
        status, _, _ = self.request("POST", "/api/disconnect", headers={"X-CFTP-Request": ""})
        self.assertEqual(status, 403)

    def test_foreign_origin_and_host_are_rejected(self):
        for headers in ({"Origin": "https://example.com"}, {"Host": "example.com:1234"}):
            self.assertEqual(self.request("GET", "/api/status", headers=headers)[0], 403)

    def test_connection_settings_validation(self):
        for body in ({"host": "", "port": 9000}, {"host": "127.0.0.1", "port": -1},
                     {"host": "127.0.0.1", "port": True}, {"host": "127.0.0.1", "port": "9000"}):
            self.assertEqual(self.request("POST", "/api/connect", body)[0], 400)

    def test_unknown_routes_and_static_traversal_return_404(self):
        for path in ("/unknown", "/../cftp/server.py", "/api/unknown"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_web_operations_reuse_one_persistent_tcp_session(self):
        self.connect()
        identity = self.web.state._client.client_id
        for _ in range(3):
            self.assertEqual(self.request("GET", "/api/files")[0], 200)
        status, _, body = self.request("GET", "/api/broadcasts")
        self.assertEqual(status, 200)
        peers = json.loads(body)["clients"]
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["id"], identity)

    def test_web_broadcast_is_received_verified_and_downloadable(self):
        self.connect()
        filename = "broadcast-" + uuid.uuid4().hex + ".bin"
        content = bytes(range(256)) * 129
        self.assertEqual(self.request("POST", "/api/upload?filename=" + filename, content)[0], 200)
        status, _, body = self.request("POST", "/api/broadcast", {"filename": filename})
        self.assertEqual(status, 202, body)
        job = json.loads(body)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, body = self.request("GET", "/api/broadcasts")
            data = json.loads(body)
            if data["jobs"][0]["status"] != "sending":
                break
            time.sleep(0.03)
        self.assertEqual(data["jobs"][0]["verified"], 1)
        self.assertTrue(data["inbox"][0]["success"])
        self.assertNotIn("path", data["inbox"][0])
        status, headers, payload = self.request("GET", "/api/inbox-download?id=" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload, content)
        self.assertEqual(headers["X-CFTP-Checksum"], hashlib.sha256(content).hexdigest())

    def test_broadcast_inbox_keeps_distinct_versions_of_same_filename(self):
        self.connect()
        filename = "versioned-" + uuid.uuid4().hex + ".txt"
        jobs = []
        for content in (b"first version", b"second version"):
            self.request("POST", "/api/upload?filename=" + filename, content)
            status, _, body = self.request("POST", "/api/broadcast", {"filename": filename})
            job = json.loads(body)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(entry["id"] == job["id"] for entry in self.web.state.inbox):
                    break
                time.sleep(0.03)
            jobs.append((job["id"], content))
        for job_id, content in jobs:
            status, _, payload = self.request("GET", "/api/inbox-download?id=" + job_id)
            self.assertEqual(status, 200)
            self.assertEqual(payload, content)


if __name__ == "__main__":
    unittest.main()
