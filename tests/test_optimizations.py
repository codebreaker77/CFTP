"""Checks that cached metadata remains correct after replacement and edits."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cftp.integrity import calculate_bytes_sha256
from cftp.storage import StorageManager


class TestChecksumCache(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = StorageManager(self.directory.name)
        self.path = Path(self.directory.name) / "data.txt"
        self.path.write_bytes(b"original")

    def test_repeated_metadata_uses_cached_digest(self):
        first = self.storage.get_file_info("data.txt")
        with patch("cftp.storage.StreamingHasher", side_effect=AssertionError("Unexpected rehash")):
            second = self.storage.get_file_info("data.txt")
            third = self.storage.list_files()[0]
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["sha256"], third["sha256"])

    def test_external_edit_invalidates_cache(self):
        first = self.storage.get_file_info("data.txt")
        old_stat = self.path.stat()
        self.path.write_bytes(b"modified")
        os.utime(self.path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1000000000))
        second = self.storage.get_file_info("data.txt")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["sha256"], calculate_bytes_sha256(b"modified"))

    def test_verified_upload_primes_cache_after_atomic_replacement(self):
        self.storage.get_file_info("data.txt")
        staged = self.storage.get_temp_path("data.txt")
        staged.write_bytes(b"replacement")
        checksum = calculate_bytes_sha256(b"replacement")
        self.storage.commit_temp_file(staged, "data.txt", checksum=checksum)
        with patch("cftp.storage.StreamingHasher", side_effect=AssertionError("Unexpected rehash")):
            info = self.storage.get_file_info("data.txt")
        self.assertEqual(info["sha256"], checksum)
        self.assertEqual(info["size"], 11)


if __name__ == "__main__":
    unittest.main()
