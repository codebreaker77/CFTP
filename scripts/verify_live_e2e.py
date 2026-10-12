"""End-to-end live multi-process verification script for CFTP.

Verifies:
1. Server process startup and client connection across TCP.
2. CLI sub-commands in separate client processes: list, put, get.
3. Interactive/Library commands: LIST, PUT, GET, QUIT.
4. Edge Case: Missing files (404 NOT_FOUND).
5. Edge Case: Empty 0-byte files (PUT and GET without hang).
6. Edge Case: Binary files (128 KB random bytes, SHA-256 match).
7. Edge Case: Fragmented TCP reads (byte-by-byte delivery and reassembly).
8. Edge Case: Interrupted transfers (mid-payload disconnect and temporary staging file cleanup).
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cftp.client import CFTPClient
from cftp.config import Config
from cftp.integrity import calculate_bytes_sha256, calculate_file_sha256
from cftp.protocol import (
    STATUS_NOT_FOUND,
    STATUS_OK,
    FramedSocket,
    Request,
    Response,
)


def log(msg: str) -> None:
    print(f"[TEST RUNNER] {msg}", flush=True)


def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    return False


def run_e2e_verification() -> bool:
    all_passed = True
    test_port = 9099
    host = "127.0.0.1"

    temp_dir = Path(tempfile.mkdtemp(prefix="cftp_e2e_"))
    server_dir = temp_dir / "server_storage"
    client_dir = temp_dir / "client_storage"
    server_dir.mkdir(parents=True, exist_ok=True)
    client_dir.mkdir(parents=True, exist_ok=True)
    server_log_file = temp_dir / "server.log"

    log(f"Created isolated test directories under: {temp_dir}")

    # Start server in a separate process
    log(f"Starting CFTP server on {host}:{test_port} in separate process...")
    server_log_handle = open(server_log_file, "w", encoding="utf-8")
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cftp.server",
            "--host",
            host,
            "--port",
            str(test_port),
            "--storage-dir",
            str(server_dir),
            "--log-level",
            "DEBUG",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        ready = wait_for_server(host, test_port, timeout=5.0)
        if not ready:
            log("FAIL: Server failed to start or accept connections within 5 seconds.")
            return False
        log("PASS: Server process is listening and responding to TCP connections.")

        # ==========================================
        # 1. Test CLI Commands via subprocess
        # ==========================================
        log("--- TEST 1: CLI Subcommands (Separate Client Processes) ---")
        cli_sample_file = client_dir / "cli_sample.txt"
        cli_sample_file.write_text("Hello from CLI Subprocess!", encoding="utf-8")

        # CLI PUT
        res_put = subprocess.run(
            [
                sys.executable,
                "-m",
                "cftp.client",
                "--host",
                host,
                "--port",
                str(test_port),
                "--download-dir",
                str(client_dir),
                "put",
                str(cli_sample_file),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if res_put.returncode == 0 and "SUCCESS" in res_put.stdout:
            log("PASS: CLI 'put' succeeded.")
        else:
            log(f"FAIL: CLI 'put' failed. Output:\n{res_put.stdout}\n{res_put.stderr}")
            all_passed = False

        # CLI LIST
        res_list = subprocess.run(
            [
                sys.executable,
                "-m",
                "cftp.client",
                "--host",
                host,
                "--port",
                str(test_port),
                "--download-dir",
                str(client_dir),
                "list",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if res_list.returncode == 0 and "cli_sample.txt" in res_list.stdout:
            log("PASS: CLI 'list' verified file on server.")
        else:
            log(f"FAIL: CLI 'list' failed. Output:\n{res_list.stdout}\n{res_list.stderr}")
            all_passed = False

        # CLI GET
        cli_dest_file = client_dir / "cli_downloaded.txt"
        res_get = subprocess.run(
            [
                sys.executable,
                "-m",
                "cftp.client",
                "--host",
                host,
                "--port",
                str(test_port),
                "--download-dir",
                str(client_dir),
                "get",
                "cli_sample.txt",
                "--dest",
                str(cli_dest_file),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if res_get.returncode == 0 and cli_dest_file.exists() and cli_dest_file.read_text("utf-8") == "Hello from CLI Subprocess!":
            log("PASS: CLI 'get' downloaded file and verified content.")
        else:
            log(f"FAIL: CLI 'get' failed. Output:\n{res_get.stdout}\n{res_get.stderr}")
            all_passed = False

        # ==========================================
        # 2. Test Core Commands & QUIT Handshake
        # ==========================================
        log("--- TEST 2: Core Commands (LIST, PUT, GET, QUIT) via Client Session ---")
        client = CFTPClient(host=host, port=test_port, download_dir=client_dir)
        client.connect()
        try:
            items = client.list_files()
            log(f"PASS: LIST returned {len(items)} file(s).")

            test_file = client_dir / "api_test.txt"
            test_file.write_text("API text content test\nLine 2", encoding="utf-8")
            t_put = client.put_file(test_file)
            assert t_put.success and t_put.integrity_matched, "PUT failed"
            log(f"PASS: PUT 'api_test.txt' completed with SHA-256 match: {t_put.actual_sha256[:16]}...")

            t_get = client.get_file("api_test.txt", local_destination=client_dir / "api_test_get.txt")
            assert t_get.success and t_get.integrity_matched, "GET failed"
            log("PASS: GET 'api_test.txt' completed with SHA-256 match.")

            client.quit()
            log("PASS: QUIT command executed and connection terminated cleanly.")
        except Exception as e:
            log(f"FAIL: Core API test raised exception: {e}")
            all_passed = False

        # ==========================================
        # 3. Test Missing File (404 Not Found)
        # ==========================================
        log("--- TEST 3: Missing File (404 NOT_FOUND) ---")
        client = CFTPClient(host=host, port=test_port, download_dir=client_dir)
        client.connect()
        try:
            missing_result = client.get_file("definitely_not_existing_file_9999.xyz")
            if not missing_result.success and "404" in (missing_result.error_message or ""):
                log(f"PASS: Missing file correctly rejected with 404: {missing_result.error_message}")
            else:
                log(f"FAIL: Missing file did not report 404. Success={missing_result.success}, Error={missing_result.error_message}")
                all_passed = False
            client.quit()
        except Exception as e:
            log(f"FAIL: Missing file test raised exception: {e}")
            all_passed = False

        # ==========================================
        # 4. Test Empty File (0 Bytes)
        # ==========================================
        log("--- TEST 4: Empty File (0-byte payload) ---")
        empty_local = client_dir / "zero_length.empty"
        empty_local.write_bytes(b"")
        client = CFTPClient(host=host, port=test_port, download_dir=client_dir)
        client.connect()
        try:
            put_empty = client.put_file(empty_local)
            if put_empty.success and put_empty.bytes_transferred == 0:
                log("PASS: Empty 0-byte file uploaded successfully.")
            else:
                log(f"FAIL: Empty file upload failed: {put_empty.error_message}")
                all_passed = False

            get_empty_dest = client_dir / "zero_downloaded.empty"
            get_empty = client.get_file("zero_length.empty", local_destination=get_empty_dest)
            if get_empty.success and get_empty_dest.exists() and get_empty_dest.stat().st_size == 0:
                log("PASS: Empty 0-byte file downloaded successfully without hanging.")
            else:
                log(f"FAIL: Empty file download failed: {get_empty.error_message}")
                all_passed = False
            client.quit()
        except Exception as e:
            log(f"FAIL: Empty file test raised exception: {e}")
            all_passed = False

        # ==========================================
        # 5. Test Binary File (128 KB Random Bytes)
        # ==========================================
        log("--- TEST 5: Binary File (128 KB with Null Bytes & High Bytes) ---")
        bin_data = os.urandom(128 * 1024)
        bin_local = client_dir / "random_binary.bin"
        bin_local.write_bytes(bin_data)
        bin_hash = calculate_bytes_sha256(bin_data)

        client = CFTPClient(host=host, port=test_port, download_dir=client_dir)
        client.connect()
        try:
            put_bin = client.put_file(bin_local)
            if put_bin.success and put_bin.integrity_matched and put_bin.actual_sha256 == bin_hash:
                log(f"PASS: Binary file upload verified: SHA-256={bin_hash[:16]}... bytes={len(bin_data)}")
            else:
                log(f"FAIL: Binary upload failed: {put_bin.error_message}")
                all_passed = False

            bin_download_dest = client_dir / "downloaded_binary.bin"
            get_bin = client.get_file("random_binary.bin", local_destination=bin_download_dest)
            if get_bin.success and get_bin.integrity_matched and bin_download_dest.read_bytes() == bin_data:
                log("PASS: Binary file downloaded and verified bit-for-bit identical.")
            else:
                log(f"FAIL: Binary download failed: {get_bin.error_message}")
                all_passed = False
            client.quit()
        except Exception as e:
            log(f"FAIL: Binary file test raised exception: {e}")
            all_passed = False

        # ==========================================
        # 6. Test Fragmented TCP Reads
        # ==========================================
        log("--- TEST 6: Fragmented TCP Reads (Byte-by-Byte Delivery) ---")
        try:
            raw_sock = socket.create_connection((host, test_port), timeout=5.0)
            framed = FramedSocket(raw_sock)

            # Send command byte-by-byte with small delays
            command_wire = b"CFTP/1.0 LIST\r\n"
            for b in command_wire:
                raw_sock.sendall(bytes([b]))
                time.sleep(0.002)

            resp_line = framed.read_line()
            assert resp_line is not None, "Server did not respond to fragmented request"
            resp = Response.parse(resp_line)
            assert resp.code == STATUS_OK, f"Expected 200, got {resp.code}"

            size = int(resp.params.get("SIZE", "0"))
            payload = framed.read_exact(size)
            catalog = json.loads(payload.decode("utf-8"))
            log(f"PASS: Server reassembled fragmented LIST request and returned catalog ({len(catalog)} entries).")

            # Send QUIT split across 2 chunks
            raw_sock.sendall(b"CFTP/1.0 ")
            time.sleep(0.01)
            raw_sock.sendall(b"QUIT\r\n")

            quit_resp_line = framed.read_line()
            assert quit_resp_line is not None
            quit_resp = Response.parse(quit_resp_line)
            assert quit_resp.code == STATUS_OK
            log("PASS: Server reassembled split QUIT command and acknowledged.")
            framed.close()
        except Exception as e:
            log(f"FAIL: Fragmented TCP reads test raised exception: {e}")
            all_passed = False

        # ==========================================
        # 7. Test Interrupted Transfer & Staging Cleanup
        # ==========================================
        log("--- TEST 7: Interrupted Transfer (Premature EOF & Staging Cleanup) ---")
        try:
            raw_sock = socket.create_connection((host, test_port), timeout=5.0)
            framed = FramedSocket(raw_sock)

            target_filename = "interrupted_demo.bin"
            declared_size = 50000
            fake_hash = "a" * 64
            req = Request(
                command="PUT",
                argument=target_filename,
                params={"SIZE": str(declared_size), "CHECKSUM": fake_hash},
            )
            framed.send_line(req.to_wire())

            # Read server 200 Ready response
            resp_line = framed.read_line()
            assert resp_line is not None
            resp = Response.parse(resp_line)
            assert resp.code == STATUS_OK, f"Expected 200 Ready, got {resp.code}"

            # Send only 500 bytes then abruptly abort the connection
            partial_payload = b"X" * 500
            raw_sock.sendall(partial_payload)
            time.sleep(0.05)
            # Hard close
            raw_sock.close()
            log("Sent 500 of 50000 bytes and closed socket abruptly.")

            time.sleep(0.3)  # Allow server thread to handle EOF and clean up

            # Check server storage directory
            committed_file = server_dir / target_filename
            temp_files = list(server_dir.glob("*.cftp_part"))

            if committed_file.exists():
                log("FAIL: Interrupted file was committed to server storage!")
                all_passed = False
            else:
                log("PASS: Incomplete file was NOT committed to server directory.")

            if temp_files:
                log(f"FAIL: Temporary staging files left behind: {[f.name for f in temp_files]}")
                all_passed = False
            else:
                log("PASS: All temporary staging files (*.cftp_part) cleanly purged by server.")
        except Exception as e:
            log(f"FAIL: Interrupted transfer test raised exception: {e}")
            all_passed = False

    finally:
        log("Shutting down server process...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        server_log_handle.close()
        log("Server process terminated.")

        # Clean up test directories
        shutil.rmtree(temp_dir, ignore_errors=True)
        log("Cleaned up temporary directories.")

    return all_passed


if __name__ == "__main__":
    success = run_e2e_verification()
    if success:
        log("ALL END-TO-END VERIFICATION CHECKS PASSED SUCCESSFULLY!")
        sys.exit(0)
    else:
        log("SOME CHECKS FAILED.")
        sys.exit(1)
