"""Regression coverage for framing, transfer integrity, and server lifecycle."""

import io
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cftp.client import CFTPClient
from cftp.integrity import calculate_bytes_sha256
from cftp.protocol import FramedSocket, FramingError, ProtocolError, Request, Response
from cftp.server import CFTPServer
from cftp.storage import StorageManager, StorageSecurityError


class TestProtocolRegressions(unittest.TestCase):
    def test_terminated_oversized_control_line(self):
        sock = Mock()
        sock.recv.return_value = b"A" * 129 + b"\r\n"
        with self.assertRaises(FramingError):
            FramedSocket(sock, max_line_length=128).read_line()

    def test_limit_allows_fragmented_crlf(self):
        sock = Mock()
        sock.recv.side_effect = [b"A" * 128 + b"\r", b"\n"]
        self.assertEqual(FramedSocket(sock, max_line_length=128).read_line(), "A" * 128)

    def test_filename_with_equals_roundtrip(self):
        for command in ("GET", "PUT"):
            request = Request(command=command, argument="report=final.txt",
                              params={"SIZE": "0"} if command == "PUT" else {})
            parsed = Request.parse(request.to_wire())
            self.assertEqual(parsed.argument, request.argument)
            self.assertEqual(parsed.params, request.params)

    def test_reserved_device_with_multiple_extensions(self):
        for name in ("CON.tar.gz", "aux.backup.txt", "NUL .txt", "COM1.a.b"):
            with self.subTest(name=name), self.assertRaises(StorageSecurityError):
                StorageManager.validate_filename(name)

    def test_reserved_staging_suffix_and_trailing_dot(self):
        for name in ("document.", "document.cftp_part"):
            with self.subTest(name=name), self.assertRaises(StorageSecurityError):
                StorageManager.validate_filename(name)

    def test_new_nested_storage_root_is_absolute(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            relative = Path(directory).relative_to(Path.cwd()) if Path(directory).is_absolute() else Path(directory)
            storage = StorageManager(relative / "new" / "nested")
            self.assertTrue(storage.root_dir.is_absolute())
            self.assertEqual(storage.resolve_safe_path("file.txt").parent, storage.root_dir)


class TestTransferRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_put_requires_matching_completion_checksum(self):
        source = self.root / "source.txt"
        source.write_bytes(b"upload")
        for checksum in (None, "0" * 64, "g" * 64):
            with self.subTest(checksum=checksum):
                client = CFTPClient(download_dir=self.root)
                framed = Mock()
                client._framed = framed
                params = {} if checksum is None else {"CHECKSUM": checksum}
                framed.read_line.side_effect = [
                    Response.ok(reason="READY_FOR_DATA").to_wire(),
                    Response.ok(reason="TRANSFER_COMPLETE", params=params).to_wire(),
                ]
                def stream(f, n, hasher, progress_cb):
                    if hasher is not None:
                        hasher.update(f.read(n))
                framed.stream_file_to_socket.side_effect = stream
                result = client.put_file(source)
                self.assertFalse(result.success)
                self.assertFalse(result.integrity_matched)
                self.assertIsNotNone(result.error_message)

    def test_get_invalid_metadata_disconnects(self):
        for params in ({"CHECKSUM": "0" * 64}, {"SIZE": "-1", "CHECKSUM": "0" * 64},
                       {"SIZE": "0"}, {"SIZE": "0", "CHECKSUM": "g" * 64}):
            with self.subTest(params=params):
                client = CFTPClient(download_dir=self.root)
                framed = Mock()
                client._framed = framed
                framed.read_line.return_value = Response.ok(params=params).to_wire()
                with self.assertRaises(ProtocolError):
                    client.get_file("file.txt")
                self.assertFalse(client.is_connected)
                framed.close.assert_called_once()

    def test_interrupted_get_preserves_destination_and_cleans_staging(self):
        destination = self.root / "file.txt"
        destination.write_bytes(b"original")
        client = CFTPClient(download_dir=self.root)
        framed = Mock()
        client._framed = framed
        framed.read_line.return_value = Response.ok(
            params={"SIZE": "10", "CHECKSUM": "0" * 64}).to_wire()
        framed.stream_exact_to_file.side_effect = FramingError("Incomplete payload")
        with self.assertRaises(FramingError):
            client.get_file("file.txt")
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob("*.cftp_part")), [])
        self.assertFalse(client.is_connected)

    def test_concurrent_downloads_use_distinct_staging_files(self):
        content = b"download"
        checksum = calculate_bytes_sha256(content)
        clients = [CFTPClient(download_dir=self.root) for _ in range(2)]
        barrier = threading.Barrier(2)
        staging_paths = []
        failures = []
        def stream(f, n, hasher, progress_cb):
            staging_paths.append(f.name)
            barrier.wait(timeout=3)
            f.write(content)
            hasher.update(content)
        for client in clients:
            client._framed = Mock()
            client._framed.read_line.return_value = Response.ok(
                params={"SIZE": str(len(content)), "CHECKSUM": checksum}).to_wire()
            client._framed.stream_exact_to_file.side_effect = stream
        def download(client):
            try:
                self.assertTrue(client.get_file("shared.txt").success)
            except Exception as err:
                failures.append(err)
        threads = [threading.Thread(target=download, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(set(staging_paths)), 2)
        self.assertEqual((self.root / "shared.txt").read_bytes(), content)

    def test_server_get_hashes_and_streams_one_descriptor(self):
        server = CFTPServer(storage_dir=self.root)
        original_path = self.root / "file.txt"
        original_path.write_bytes(b"original")
        original = original_path.open("rb")
        framed = Mock()
        def capture(f, n):
            self.assertIs(f, original)
            self.assertEqual(f.read(n), b"original")
        framed.stream_file_to_socket.side_effect = capture
        with patch("cftp.server.open", return_value=original) as opened, \
                patch.object(Path, "is_file", return_value=True):
            server._handle_get(Request(command="GET", argument="file.txt"), framed, ("127.0.0.1", 1))
        opened.assert_called_once()
        response = Response.parse(framed.send_line.call_args.args[0])
        self.assertEqual(response.params["SIZE"], "8")
        self.assertEqual(response.params["CHECKSUM"], calculate_bytes_sha256(b"original"))

    def test_server_get_failure_after_header_terminates_session(self):
        server = CFTPServer(storage_dir=self.root)
        (self.root / "file.txt").write_bytes(b"content")
        framed = Mock()
        framed.stream_file_to_socket.side_effect = FramingError("Transfer interrupted")
        with self.assertRaises(FramingError):
            server._handle_get(Request(command="GET", argument="file.txt"), framed, ("127.0.0.1", 1))
        framed.send_line.assert_called_once()

    def test_server_rejects_nonhex_upload_checksum_before_payload(self):
        server = CFTPServer(storage_dir=self.root)
        framed = Mock()
        server._handle_put(Request(command="PUT", argument="file.txt",
                                  params={"SIZE": "0", "CHECKSUM": "g" * 64}), framed, ("127.0.0.1", 1))
        self.assertEqual(Response.parse(framed.send_line.call_args.args[0]).code, 400)
        framed.stream_exact_to_file.assert_not_called()


class TestServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = CFTPServer(port=0, storage_dir=self.directory.name, max_clients=1)
        self.thread = threading.Thread(target=self.server.start, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        deadline = time.monotonic() + 3
        while not self.server._is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server._is_running)

    def shutdown(self):
        self.server.stop()
        self.thread.join(timeout=3)

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        self.addCleanup(sock.close)
        return sock

    def test_stop_closes_idle_clients_and_releases_threads(self):
        sock = self.connect()
        framed = FramedSocket(sock)
        framed.send_line(Request(command="LIST").to_wire())
        response = Response.parse(framed.read_line())
        framed.read_exact(int(response.params["SIZE"]))
        self.shutdown()
        self.assertFalse(self.thread.is_alive())
        self.assertIsNone(framed.read_line())
        self.assertEqual(self.server._client_threads, [])
        self.assertEqual(self.server._client_sockets, set())

    def test_connection_limit_and_finished_worker_cleanup(self):
        first = FramedSocket(self.connect())
        first.send_line(Request(command="LIST").to_wire())
        response = Response.parse(first.read_line())
        first.read_exact(int(response.params["SIZE"]))
        second = self.connect()
        try:
            self.assertEqual(second.recv(1), b"")
        except ConnectionResetError:
            pass
        first.send_line(Request(command="QUIT").to_wire())
        self.assertEqual(Response.parse(first.read_line()).reason, "GOODBYE")
        deadline = time.monotonic() + 2
        while self.server._client_threads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server._client_threads, [])
        self.assertEqual(self.server._client_sockets, set())


if __name__ == "__main__":
    unittest.main()
