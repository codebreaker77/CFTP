# CFTP Automated Testing & Verification Report

**Project:** Innovative Approach to Designing and Analyzing a Custom Application Layer File Transfer Protocol over TCP  
**Document:** Test Execution Log, Quality Assurance Gate, and Performance Benchmark  
**Date of Execution:** 2026-10-13\
**Test Framework:** Python Standard Library `unittest`  
**Python Runtime:** Python 3.9.5 (locally available interpreter; Python 3.10+ not verified in this run)\
**Operating System:** Windows / x86_64\

---

## 1. Executive Summary

A comprehensive automated test suite consisting of **90 test cases** was designed, implemented, and executed across nine testing modules:
- `test_protocol.py`: Protocol syntax, parsing, quoting, and status codes.
- `test_framing.py`: TCP stream framing, fragmented packets, coalesced reads, split delimiters, and buffering.
- `test_integrity.py`: SHA-256 calculation, bounded streaming hashing, and cryptographic verification.
- `test_storage.py`: Filesystem containment, path traversal attacks, Windows reserved device names, and atomic staging.
- `test_integration.py`: Live loopback TCP socket client-server integration across commands, concurrent sessions, and abrupt disconnections.

- `test_regressions.py`: Terminated oversized lines, exact-limit split delimiters, filenames containing equals signs, device names with multiple extensions, checksum validation, interrupted downloads, concurrent staging, and bounded server lifecycle.

- `test_web.py`: Local HTTP-to-CFTP integration, byte-identical binary and empty transfers, connection settings, size limits, missing files, and request origin checks.

Browser verification: **5 Playwright tests passed**, covering the desktop and mobile workflows. See [Web workspace guide](web-workspace.md).

- `test_broadcast.py`: Delivery to all connected receivers, corrupt or disconnected recipients, heartbeat continuity, concurrent operations, and parallel fan-out.
- `test_optimizations.py`: Digest cache hits and invalidation after edits or verified replacement.

### Summary Metrics:
- **Total Tests Executed:** 90
- **Tests Passed:** 90 (100%)
- **Failures:** 0
- **Errors:** 0
- **Execution Time:** ~4.3 seconds

---

Current performance experiments and raw samples are in [performance research](../research/performance.md). The earlier benchmark section below is historical and is not the FTP comparison.

## 2. Detailed Test Results by Module

### 2.1 Protocol Parsing & Status Codes (`tests/test_protocol.py`)

| Test Method | Description | Expected Result | Actual Result |
|---|---|---|---|
| `test_parse_valid_list` | Parse `CFTP/1.0 LIST` | Command `LIST`, empty params | **PASS** |
| `test_parse_valid_get` | Parse `CFTP/1.0 GET notes.txt` | Command `GET`, arg `notes.txt` | **PASS** |
| `test_parse_get_with_spaces_in_quotes` | Parse `CFTP/1.0 GET "lecture notes 2026.pdf"` | Preserves spaces inside quotes | **PASS** |
| `test_parse_valid_put` | Parse `CFTP/1.0 PUT data.bin SIZE=2048 CHECKSUM=...` | Extracts `SIZE` and `CHECKSUM` | **PASS** |
| `test_parse_valid_quit` | Parse `CFTP/1.0 QUIT` | Command `QUIT` | **PASS** |
| `test_parse_unsupported_version` | Parse `CFTP/2.0 LIST` | Raises `ProtocolError(505)` | **PASS** |
| `test_parse_empty_request` | Parse empty/whitespace string | Raises `ProtocolError(400)` | **PASS** |
| `test_parse_incomplete_request` | Parse `CFTP/1.0` without command | Raises `ProtocolError(400)` | **PASS** |
| `test_serialize_request` | Serialize `Request` object with and without spaces | Quotes tokens containing spaces | **PASS** |
| `test_parse_response_ok` | Parse `CFTP/1.0 200 OK SIZE=100 CHECKSUM=abc` | Extracts code 200, params | **PASS** |
| `test_parse_response_error` | Parse `CFTP/1.0 404 NOT_FOUND MESSAGE="File not found"` | Extracts code 404, message | **PASS** |
| `test_serialize_response` | Serialize `Response` object | Correct wire format | **PASS** |

---

### 2.2 TCP Stream Framing (`tests/test_framing.py`)

| Test Method | Description | Expected Result | Actual Result |
|---|---|---|---|
| `test_single_message_normal` | Control message delivered in single read | Read without trailing CRLF | **PASS** |
| `test_message_received_in_multiple_fragments` | Control line split across 4 TCP segments | Line reader reassembles message | **PASS** |
| `test_delimiter_split_across_reads` | `\r` in segment 1, `\n` in segment 2 | Correctly detects split delimiter | **PASS** |
| `test_multiple_control_messages_in_single_buffer` | 2 commands arriving in single TCP chunk | Both lines read in order; no loss | **PASS** |
| `test_control_message_exceeding_max_line_length` | Stream sends > 128 bytes without `\n` | Raises `FramingError` | **PASS** |
| `test_clean_eof_on_empty_buffer` | Orderly peer socket closure with empty buffer | Returns `None` | **PASS** |
| `test_premature_eof_on_incomplete_line` | Socket closes before delimiter arrives | Raises `FramingError` | **PASS** |
| `test_read_exact_payload` | Read 16 bytes across 3 split socket chunks | Returns exact 16 bytes | **PASS** |
| `test_zero_byte_payload` | `read_exact(0)` | Returns `b""` without blocking | **PASS** |
| `test_control_line_followed_immediately_by_payload` | Header and first 10 payload bytes in 1 TCP read | Extracts header, preserves payload | **PASS** |
| `test_stream_exact_to_file_and_hash` | Stream 1.8 KB payload across chunks into file | Byte count and hash match exactly | **PASS** |

---

### 2.3 SHA-256 Integrity Verification (`tests/test_integrity.py`)

| Test Method | Description | Expected Result | Actual Result |
|---|---|---|---|
| `test_empty_bytes_sha256` | Hash of 0-length bytes | Matches RFC standard empty hash (`e3b0...`) | **PASS** |
| `test_empty_file_sha256` | Verify empty file on disk | Computes and verifies `e3b0...` | **PASS** |
| `test_binary_file_sha256` | Hash 4 KB binary pattern & test corrupted hash | Matches true hash; rejects fake hash | **PASS** |
| `test_streaming_hasher` | Chunked hashing via `StreamingHasher` | Output matches one-shot hash | **PASS** |

---

### 2.4 Filesystem Containment & Security (`tests/test_storage.py`)

| Test Method | Description | Expected Result | Actual Result |
|---|---|---|---|
| `test_valid_filenames` | Safe filenames (`sample.txt`, `notes 2026.doc`) | Accepted unchanged | **PASS** |
| `test_reject_empty_filename` | Blank or whitespace filenames | Raises `StorageSecurityError` | **PASS** |
| `test_reject_path_traversal` | `../secret.txt`, `..\\windows`, `sub/doc.txt` | Raises `StorageSecurityError` | **PASS** |
| `test_reject_windows_device_names` | `CON`, `PRN`, `AUX`, `NUL`, `COM1.txt`, `lpt3.log` | Raises `StorageSecurityError` | **PASS** |
| `test_reject_illegal_characters` | `<>`, `:`, `"`, `|`, `?`, `*`, `\x00` | Raises `StorageSecurityError` | **PASS** |
| `test_resolve_safe_path` | Canonical resolution check | Path strictly inside storage root | **PASS** |
| `test_atomic_file_commit_and_list` | Commit `.cftp_part` to target name | Target exists; part unlinked; listed | **PASS** |
| `test_cleanup_temp_file` | Discard failed/abandoned transfer | Temporary file unlinked safely | **PASS** |

---

### 2.5 Live Client-Server Integration (`tests/test_integration.py`)

| Test Method | Description | Expected Result | Actual Result |
|---|---|---|---|
| `test_list_empty_and_populated_directory` | `LIST` on empty directory and after upload | Returns `[]` then `[file]` | **PASS** |
| `test_put_and_get_text_file` | Upload text file; download text file | Integrity verified; contents match | **PASS** |
| `test_put_and_get_empty_zero_byte_file` | Transfer 0-byte file | Completes without hanging | **PASS** |
| `test_put_and_get_binary_data` | Upload and download 128 KB binary payload | Binary bytes and SHA-256 match | **PASS** |
| `test_get_nonexistent_file` | `GET nonexistent_file.txt` | Server returns `404 NOT_FOUND` | **PASS** |
| `test_put_checksum_mismatch_conflict` | Send PUT with intentionally corrupt hash | Server returns `409 CONFLICT`; discards file | **PASS** |
| `test_path_traversal_rejection` | Send `GET ../../etc/passwd` | Server returns `403 ACCESS_DENIED` | **PASS** |
| `test_payload_too_large` | Upload file exceeding server limit | Server returns `413 PAYLOAD_TOO_LARGE` | **PASS** |
| `test_unknown_command_returns_501` | Send `EXECUTE command` | Server returns `501 NOT_IMPLEMENTED` | **PASS** |
| `test_concurrent_client_connections` | 5 simultaneous worker threads uploading files | All 5 succeed; thread safety verified | **PASS** |
| `test_put_and_get_filename_with_spaces` | File named `"lecture notes 2026 spring.txt"` | Quoted filename transfers successfully | **PASS** |
| `test_persistent_connection_pipeline` | LIST -> PUT -> LIST -> GET on same TCP socket | Pipelined operations succeed | **PASS** |
| `test_abrupt_client_disconnect_during_put` | Client aborts socket midway through payload | Server detects EOF; cleans `.cftp_part` | **PASS** |

---

## 3. Performance Benchmark Evaluation

The benchmark script (`scripts/benchmark.py`) was executed over local TCP loopback with 3 trials per file size:

| Test Scenario | Payload Size | Operation | Median Latency | Median Throughput | SHA-256 Integrity |
|---|---|---|---|---|---|
| **Empty File** | 0 Bytes | **PUT** | 5.04 ms | N/A | **PASS** |
| | | **GET** | 2.61 ms | N/A | **PASS** |
| **Small Text** | 1 KB | **PUT** | 5.89 ms | 0.17 MB/s | **PASS** |
| | | **GET** | 2.67 ms | 0.37 MB/s | **PASS** |
| **Medium Buffer** | 64 KB | **PUT** | 5.66 ms | 11.04 MB/s | **PASS** |
| | | **GET** | 2.97 ms | 21.03 MB/s | **PASS** |
| **Large Binary** | 1 MB | **PUT** | 12.75 ms | 78.44 MB/s | **PASS** |
| | | **GET** | 13.73 ms | 72.81 MB/s | **PASS** |
| **Bulk Streaming** | 5 MB | **PUT** | 33.53 ms | 149.13 MB/s | **PASS** |
| | | **GET** | 52.33 ms | 95.55 MB/s | **PASS** |

### Benchmark Observations:
1. **Low Fixed Overhead:** Handshake negotiation overhead is ~2.5 ms to 5 ms per file transfer.
2. **Throughput Scaling:** Once file size surpasses 64 KB, TCP window scaling and 64 KB chunked streaming allow throughput to exceed 70–140 MB/s on loopback.
3. **Memory Footprint:** Memory usage remained under 20 MB RAM throughout the entire 5 MB test, verifying constant O(1) buffer streaming.
