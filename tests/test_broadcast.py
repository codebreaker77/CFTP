"""Real TCP broadcast delivery, framing, acknowledgments, and concurrency."""

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cftp.client import CFTPClient
from cftp.protocol import ConnectionClosedError, ProtocolError
from cftp.server import CFTPServer


class TestBroadcast(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.server = CFTPServer(port=0, storage_dir=self.root / "server", timeout=3)
        self.worker = threading.Thread(target=self.server.start, daemon=True)
        self.worker.start()
        deadline = time.monotonic() + 3
        while not self.server._is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.quit()
        self.server.stop()
        self.worker.join(timeout=3)
        self.directory.cleanup()

    def client(self, receives=True):
        client = CFTPClient(port=self.server.port, download_dir=self.root / f"client-{len(self.clients)}",
                            receive_broadcasts=receives, client_name=f"Test client {len(self.clients)}")
        self.clients.append(client)
        client.connect()
        return client

    def wait(self, sender, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = sender.broadcast_status(job["id"])
            if status["status"] != "sending":
                return status
            time.sleep(0.03)
        self.fail("Broadcast did not finish")

    def test_every_connected_client_including_sender_receives_verified_bytes(self):
        clients = [self.client() for _ in range(3)]
        content = bytes(range(256)) * 2048
        (self.server.storage.root_dir / "release=one.bin").write_bytes(content)
        result = self.wait(clients[0], clients[0].broadcast_file("release=one.bin"))
        self.assertEqual((result["total"], result["verified"], result["failed"]), (3, 3, 0))
        self.assertEqual(result["status"], "completed")
        for client in clients:
            self.assertEqual((client.download_dir / "release=one.bin").read_bytes(), content)
            self.assertTrue(client.received_broadcasts[0]["integrity_matched"])
            self.assertTrue(client.is_connected)

    def test_empty_file_broadcast(self):
        first, second = self.client(), self.client()
        (self.server.storage.root_dir / "empty.txt").write_bytes(b"")
        result = self.wait(first, first.broadcast_file("empty.txt"))
        self.assertEqual(result["verified"], 2)
        self.assertEqual((second.download_dir / "empty.txt").read_bytes(), b"")

    def test_disabled_and_disconnected_clients_are_not_targets(self):
        sender, disabled, gone = self.client(), self.client(False), self.client()
        gone.quit()
        (self.server.storage.root_dir / "data.txt").write_bytes(b"data")
        result = self.wait(sender, sender.broadcast_file("data.txt"))
        self.assertEqual(result["total"], 1)
        self.assertFalse((disabled.download_dir / "data.txt").exists())

    def test_corrupt_delivery_is_failed_and_does_not_replace_local_file(self):
        sender, receiver = self.client(), self.client()
        (self.server.storage.root_dir / "data.txt").write_bytes(b"data")
        (receiver.download_dir / "data.txt").write_bytes(b"keep this")
        session = next(item for item in self.server._sessions.values() if item.client_id == receiver.client_id)
        def corrupt(source, size):
            session.framed.sock.sendall(b"X" * size)
        with patch.object(session.framed, "stream_file_to_socket", side_effect=corrupt):
            result = self.wait(sender, sender.broadcast_file("data.txt"))
        self.assertEqual((result["verified"], result["failed"]), (1, 1))
        self.assertEqual((receiver.download_dir / "data.txt").read_bytes(), b"keep this")
        self.assertFalse(receiver.received_broadcasts[0]["success"])

    def test_dropped_receiver_does_not_block_other_recipients(self):
        sender, receiver = self.client(), self.client()
        (self.server.storage.root_dir / "data.txt").write_bytes(b"data")
        session = next(item for item in self.server._sessions.values() if item.client_id == receiver.client_id)
        with patch.object(session.framed, "stream_file_to_socket", side_effect=ConnectionClosedError("disconnected")):
            result = self.wait(sender, sender.broadcast_file("data.txt"))
        self.assertEqual((result["verified"], result["failed"]), (1, 1))
        self.assertEqual((sender.download_dir / "data.txt").read_bytes(), b"data")

    def test_broadcast_during_normal_uploads_and_downloads_preserves_frames(self):
        sender, receiver = self.client(), self.client()
        content = os.urandom(512 * 1024)
        (self.server.storage.root_dir / "broadcast.bin").write_bytes(content)
        local = receiver.download_dir / "source.bin"
        local.write_bytes(os.urandom(64 * 1024))
        errors = []
        def transfers():
            try:
                for index in range(5):
                    self.assertTrue(receiver.put_file(local, f"normal-{index}.bin").success)
                    self.assertTrue(receiver.get_file(f"normal-{index}.bin").success)
                    receiver.list_files()
            except Exception as err:
                errors.append(err)
        worker = threading.Thread(target=transfers)
        worker.start()
        result = self.wait(sender, sender.broadcast_file("broadcast.bin"))
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["verified"], 2)
        self.assertEqual((receiver.download_dir / "broadcast.bin").read_bytes(), content)

    def test_missing_file_and_unknown_job_have_explicit_errors(self):
        sender = self.client()
        with self.assertRaises(ProtocolError) as error:
            sender.broadcast_file("missing.txt")
        self.assertEqual(error.exception.code, 404)
        with self.assertRaises(ProtocolError):
            sender.broadcast_status("missing")

    def test_idle_receiver_heartbeat_keeps_same_session_alive(self):
        self.server.timeout = 0.5
        receiver = self.client()
        identity = receiver.client_id
        time.sleep(1.2)
        self.assertTrue(receiver.is_connected)
        receiver.list_files()
        self.assertEqual(receiver.client_id, identity)

    def test_busy_receiver_does_not_stall_parallel_delivery(self):
        sender, busy = self.client(), self.client()
        (self.server.storage.root_dir / "data.txt").write_bytes(b"data")
        session = next(item for item in self.server._sessions.values() if item.client_id == busy.client_id)
        session.send_lock.acquire()
        try:
            job = sender.broadcast_file("data.txt")
            deadline = time.monotonic() + 2
            while not sender.received_broadcasts and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(sender.received_broadcasts)
        finally:
            session.send_lock.release()
        self.assertEqual(self.wait(sender, job)["verified"], 2)


if __name__ == "__main__":
    unittest.main()
