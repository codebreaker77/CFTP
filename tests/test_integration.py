"""Integration test suite for CFTP client-server communication over TCP."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cftp.client import CFTPClient
from cftp.config import Config
from cftp.integrity import calculate_file_sha256
from cftp.protocol import (
    STATUS_ACCESS_DENIED,
    STATUS_CONFLICT,
    STATUS_NOT_FOUND,
    STATUS_PAYLOAD_TOO_LARGE,
    FramedSocket,
    Request,
    Response,
)
from cftp.server import CFTPServer


class TestCFTPIntegration(unittest.TestCase):
    """End-to-end integration tests over local TCP loopback sockets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server_dir = tempfile.TemporaryDirectory()
        cls.client_dir = tempfile.TemporaryDirectory()

        # Port 0 selects an ephemeral port allocated by the operating system
        cls.server = CFTPServer(
            host="127.0.0.1",
            port=0,
            storage_dir=cls.server_dir.name,
            max_file_size=2 * 1024 * 1024,  # 2 MB limit for tests
        )
        cls.server_thread = threading.Thread(target=cls.server.start, daemon=True)
        cls.server_thread.start()

        # Wait for server to bind and obtain actual port
        timeout = 5.0
        start = time.time()
        while cls.server.port == 0 and time.time() - start < timeout:
            time.sleep(0.05)

        cls.server_port = cls.server.port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server_dir.cleanup()
        cls.client_dir.cleanup()

    def setUp(self) -> None:
        self.client = CFTPClient(
            host="127.0.0.1",
            port=self.server_port,
            download_dir=self.client_dir.name,
            receive_broadcasts=False,  # These tests also exercise the raw framed stream.
        )
        self.client.connect()

    def tearDown(self) -> None:
        self.client.quit()

    def test_list_empty_and_populated_directory(self) -> None:
        """Verify LIST on empty directory and after files are added."""
        # Ensure clean server directory for empty test
        for item in Path(self.server_dir.name).iterdir():
            if item.is_file():
                item.unlink()

        files = self.client.list_files()
        self.assertEqual(len(files), 0)

        # Create a file directly on server storage
        test_file = Path(self.server_dir.name) / "sample_list.txt"
        test_file.write_text("Hello Listing", encoding="utf-8")

        files_after = self.client.list_files()
        self.assertEqual(len(files_after), 1)
        self.assertEqual(files_after[0]["name"], "sample_list.txt")
        self.assertEqual(files_after[0]["size"], 13)

        # Cleanup
        test_file.unlink()

    def test_put_and_get_text_file(self) -> None:
        """Verify uploading a text file and downloading it with integrity match."""
        local_src = Path(self.client_dir.name) / "source_text.txt"
        content = "Academic Project: CFTP/1.0 File Transfer Protocol"
        local_src.write_text(content, encoding="utf-8")

        # PUT to server
        put_result = self.client.put_file(local_src, remote_filename="uploaded_text.txt")
        self.assertTrue(put_result.success)
        self.assertTrue(put_result.integrity_matched)
        self.assertEqual(put_result.bytes_transferred, len(content.encode("utf-8")))

        # Verify file exists on server
        server_path = Path(self.server_dir.name) / "uploaded_text.txt"
        self.assertTrue(server_path.is_file())

        # GET back from server
        get_dest = Path(self.client_dir.name) / "downloaded_text.txt"
        get_result = self.client.get_file("uploaded_text.txt", local_destination=get_dest)
        self.assertTrue(get_result.success)
        self.assertTrue(get_result.integrity_matched)
        self.assertEqual(get_dest.read_text(encoding="utf-8"), content)

    def test_put_and_get_empty_zero_byte_file(self) -> None:
        """Verify zero-byte file transfer completes without hanging or error."""
        empty_src = Path(self.client_dir.name) / "zero_byte.empty"
        empty_src.write_bytes(b"")

        put_result = self.client.put_file(empty_src)
        self.assertTrue(put_result.success)
        self.assertTrue(put_result.integrity_matched)
        self.assertEqual(put_result.bytes_transferred, 0)

        # GET zero-byte file
        empty_dest = Path(self.client_dir.name) / "downloaded_zero.empty"
        get_result = self.client.get_file("zero_byte.empty", local_destination=empty_dest)
        self.assertTrue(get_result.success)
        self.assertTrue(get_result.integrity_matched)
        self.assertEqual(get_result.bytes_transferred, 0)
        self.assertEqual(empty_dest.stat().st_size, 0)

    def test_put_and_get_binary_data(self) -> None:
        """Verify upload and download of binary payload with full SHA-256 verification."""
        bin_data = bytes([i % 256 for i in range(131072)])  # 128 KB binary pattern
        src_bin = Path(self.client_dir.name) / "binary_test.bin"
        src_bin.write_bytes(bin_data)

        put_res = self.client.put_file(src_bin)
        self.assertTrue(put_res.success)
        self.assertTrue(put_res.integrity_matched)

        dest_bin = Path(self.client_dir.name) / "binary_downloaded.bin"
        get_res = self.client.get_file("binary_test.bin", local_destination=dest_bin)
        self.assertTrue(get_res.success)
        self.assertTrue(get_res.integrity_matched)
        self.assertEqual(dest_bin.read_bytes(), bin_data)

    def test_get_nonexistent_file(self) -> None:
        """Verify server returns 404 NOT_FOUND for nonexistent files."""
        res = self.client.get_file("nonexistent_file_xyz.txt")
        self.assertFalse(res.success)
        self.assertIn("404", res.error_message or "")

    def test_put_checksum_mismatch_conflict(self) -> None:
        """Verify server rejects corrupt upload with 409 CONFLICT / INTEGRITY_CHECK_FAILED."""
        # Manually perform handshake with intentionally corrupted checksum
        bad_checksum = "0" * 64
        req = Request(
            command="PUT",
            argument="corrupt.txt",
            params={"SIZE": "10", "CHECKSUM": bad_checksum},
        )
        self.client._framed.send_line(req.to_wire())

        ready_line = self.client._framed.read_line()
        ready_resp = Response.parse(ready_line)
        self.assertEqual(ready_resp.code, 200)

        # Send actual 10 bytes (whose sha256 is definitely not 0*64)
        self.client._framed.sock.sendall(b"1234567890")

        resp_line = self.client._framed.read_line()
        resp = Response.parse(resp_line)
        self.assertEqual(resp.code, STATUS_CONFLICT)
        self.assertIn("INTEGRITY_CHECK_FAILED", resp.reason)

        # Ensure corrupt file was not committed to storage
        server_corrupt = Path(self.server_dir.name) / "corrupt.txt"
        self.assertFalse(server_corrupt.exists())

    def test_path_traversal_rejection(self) -> None:
        """Verify server rejects path traversal attempts with 403 ACCESS_DENIED."""
        req = Request(command="GET", argument="../../etc/passwd")
        self.client._framed.send_line(req.to_wire())

        resp_line = self.client._framed.read_line()
        resp = Response.parse(resp_line)
        self.assertEqual(resp.code, STATUS_ACCESS_DENIED)

    def test_payload_too_large(self) -> None:
        """Verify server rejects uploads exceeding configured max size with 413."""
        oversized = 5 * 1024 * 1024  # 5 MB (server limit is 2 MB)
        req = Request(
            command="PUT",
            argument="huge.bin",
            params={"SIZE": str(oversized), "CHECKSUM": "a" * 64},
        )
        self.client._framed.send_line(req.to_wire())

        resp_line = self.client._framed.read_line()
        resp = Response.parse(resp_line)
        self.assertEqual(resp.code, STATUS_PAYLOAD_TOO_LARGE)

    def test_unknown_command_returns_501(self) -> None:
        """Verify server returns 501 NOT_IMPLEMENTED for unsupported commands."""
        req = Request(command="EXECUTE", argument="command")
        self.client._framed.send_line(req.to_wire())

        resp_line = self.client._framed.read_line()
        resp = Response.parse(resp_line)
        self.assertEqual(resp.code, 501)

    def test_concurrent_client_connections(self) -> None:
        """Verify server handles multiple simultaneous client sessions correctly."""
        results = []

        def client_worker(worker_id: int):
            c = CFTPClient(host="127.0.0.1", port=self.server_port, download_dir=self.client_dir.name)
            try:
                c.connect()
                # Create and put unique file
                src = Path(self.client_dir.name) / f"worker_{worker_id}.txt"
                src.write_text(f"Thread worker content {worker_id}", encoding="utf-8")
                res = c.put_file(src)
                results.append(res.success and res.integrity_matched)
            finally:
                c.quit()

        threads = [threading.Thread(target=client_worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 5)
        self.assertTrue(all(results))

        # Cleanup worker files
        for i in range(5):
            p = Path(self.server_dir.name) / f"worker_{i}.txt"
            if p.exists():
                p.unlink()

    def test_put_and_get_filename_with_spaces(self) -> None:
        """Verify handling of filenames with spaces and proper quoting."""
        filename = "lecture notes 2026 spring.txt"
        content = "Important notes on TCP framing and socket state transitions."
        src = Path(self.client_dir.name) / filename
        src.write_text(content, encoding="utf-8")

        put_res = self.client.put_file(src)
        self.assertTrue(put_res.success)
        self.assertTrue(put_res.integrity_matched)

        dest = Path(self.client_dir.name) / "downloaded_spaces.txt"
        get_res = self.client.get_file(filename, local_destination=dest)
        self.assertTrue(get_res.success)
        self.assertTrue(get_res.integrity_matched)
        self.assertEqual(dest.read_text(encoding="utf-8"), content)

        # Cleanup
        (Path(self.server_dir.name) / filename).unlink(missing_ok=True)

    def test_persistent_connection_pipeline(self) -> None:
        """Verify issuing multiple sequential operations on a single persistent TCP connection."""
        # 1. LIST
        files1 = self.client.list_files()

        # 2. PUT
        src = Path(self.client_dir.name) / "pipeline_doc.bin"
        payload = b"Persistent Connection Pipeline" * 64
        src.write_bytes(payload)
        put_res = self.client.put_file(src)
        self.assertTrue(put_res.success)

        # 3. LIST again
        files2 = self.client.list_files()
        self.assertEqual(len(files2), len(files1) + 1)

        # 4. GET
        dest = Path(self.client_dir.name) / "pipeline_get.bin"
        get_res = self.client.get_file("pipeline_doc.bin", local_destination=dest)
        self.assertTrue(get_res.success)
        self.assertEqual(dest.read_bytes(), payload)

        # Cleanup
        (Path(self.server_dir.name) / "pipeline_doc.bin").unlink(missing_ok=True)

    def test_abrupt_client_disconnect_during_put(self) -> None:
        """Verify server cleans up partial temporary file when client abruptly disconnects during upload."""
        raw_sock = None
        try:
            import socket as s_mod
            raw_sock = s_mod.socket(s_mod.AF_INET, s_mod.SOCK_STREAM)
            raw_sock.connect(("127.0.0.1", self.server_port))
            framed = FramedSocket(raw_sock)

            # Send PUT header declaring 10,000 bytes
            req = Request(
                command="PUT",
                argument="interrupted.bin",
                params={"SIZE": "10000", "CHECKSUM": "f" * 64},
            )
            framed.send_line(req.to_wire())

            # Read ready line
            ready = framed.read_line()
            self.assertIn("READY_FOR_DATA", ready)

            # Send only 500 bytes and abruptly abort connection
            raw_sock.sendall(b"X" * 500)
            raw_sock.close()
            raw_sock = None

            # Allow server a brief moment to detect socket disconnect and execute cleanup
            time.sleep(0.1)

            # Assert target file was never committed
            target = Path(self.server_dir.name) / "interrupted.bin"
            self.assertFalse(target.exists())

            # Assert no dangling .cftp_part files remain
            part_files = list(Path(self.server_dir.name).glob("*.cftp_part"))
            self.assertEqual(len(part_files), 0)
        finally:
            if raw_sock:
                raw_sock.close()


if __name__ == "__main__":
    unittest.main()
