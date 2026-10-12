# CFTP — Custom Application Layer File Transfer Protocol over TCP

> **Innovative Approach to Designing and Analyzing a Custom Application Layer File Transfer Protocol over TCP**

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests Passing](https://img.shields.io/badge/tests-90%20passed-brightgreen.svg)]()
[![Standard Library](https://img.shields.io/badge/dependencies-standard--library%20only-success.svg)]()

---

## 1. Project Overview

**CFTP (Custom File Transfer Protocol)** is an academic networking system that demonstrates the principles of designing, implementing, and analyzing a custom application-layer protocol communicating directly over a raw TCP transport stream.

Built exclusively using Python's standard `socket` library, CFTP avoids high-level web frameworks (HTTP, FTP, Flask, FastAPI) to expose the fundamental mechanics of network engineering:
- **Application Layer Design:** Custom command grammars, version negotiation (`CFTP/1.0`), and standardized 3-digit status codes.
- **TCP Stream Framing:** Solving the byte-stream reassembly problem (handling partial reads, packet coalescing, and split delimiters) without dropping adjacent messages.
- **Two-Phase Transfer Negotiation:** Strict length-prefixed data streaming with explicit byte boundaries.
- **End-to-End Cryptographic Integrity:** SHA-256 hash calculation and constant-time comparison during live streaming.
- **Filesystem Security Fence:** Path traversal prevention, Windows reserved device name neutralization, and atomic temporary staging.
- **Traffic Analysis:** Step-by-step procedures for packet inspection in **Wireshark**.

---

## Browser workspace

Start the clean, local file workspace and a CFTP server with one command:

```powershell
python -m cftp.web --demo
```

Open **http://127.0.0.1:8787** to upload, search, inspect checksums, download,
and view transfer activity. Drag-and-drop and multiple-file uploads are supported.
Files transfer through the real CFTP TCP client; no frontend build or npm install
is needed to run it. Stop the foreground server with Ctrl+C.

See [Web workspace guide](docs/web-workspace.md) for manual testing, connection
settings, alternative ports, and the automated browser suite.

To receive broadcasts in a second client:

```powershell
python -m cftp.client --download-dir downloads/client-2 listen
```

Use a file's broadcast icon in the workspace to deliver it to every connected
receiving client. See [Broadcast guide](docs/broadcasts.md) for verified delivery
and [Research](research/README.md) for the FTP comparison and reproducible benchmarks.

## 2. Protocol Syntax & Specification (`CFTP/1.0`)

CFTP control messages are UTF-8 text strings terminated by CRLF (`\r\n`).

### 2.1 Supported Commands

| Command | Format | Purpose |
|---|---|---|
| **`LIST`** | `CFTP/1.0 LIST` | Requests remote directory catalog with file sizes, timestamps, and SHA-256 hashes. |
| **`GET`** | `CFTP/1.0 GET <filename>` | Downloads `<filename>` using length-prefixed streaming with SHA-256 verification. |
| **`PUT`** | `CFTP/1.0 PUT <filename> SIZE=<n> CHECKSUM=<hash>` | Initiates two-phase file upload with server readiness acknowledgment. |
| **`BROADCAST`** | `CFTP/1.0 BROADCAST <filename>` | Starts verified delivery to every connected receiving client. |
| **`PEERS`** | `CFTP/1.0 PEERS` | Lists connected clients and reception capability. |
| **`BSTATUS`** | `CFTP/1.0 BSTATUS ID=<id>` | Reports verified and failed broadcast recipients. |
| **`QUIT`** | `CFTP/1.0 QUIT` | Gracefully closes the session with a 4-way TCP FIN handshake. |

### 2.2 Status Code Taxonomy

| Code | Reason Phrase | Meaning |
|---|---|---|
| **200** | `OK` | Request succeeded / transfer completed / ready for payload. |
| **210** | `BROADCAST` | Negotiated file event followed by its exact-length payload. |
| **400** | `BAD_REQUEST` | Syntax error, unclosed quotes, or missing parameters. |
| **403** | `ACCESS_DENIED` | Path traversal attempted (`../`), illegal character, or reserved device name. |
| **404** | `NOT_FOUND` | Target file does not exist on remote storage. |
| **409** | `CONFLICT` / `INTEGRITY_CHECK_FAILED` | Checksum mismatch; corrupted upload discarded. |
| **413** | `PAYLOAD_TOO_LARGE` | Declared file size or control message exceeds server limits. |
| **500** | `INTERNAL_SERVER_ERROR` | Unexpected server filesystem exception. |
| **501** | `NOT_IMPLEMENTED` | Command verb is unknown or unsupported. |
| **505** | `VERSION_NOT_SUPPORTED` | Protocol version specified is not `CFTP/1.0`. |

---

## 3. Project Directory Structure

```text
CN/
|-- cftp/                         # Core Python package
|   |-- __init__.py               # Exports and package metadata
|   |-- config.py                 # Protocol constants, buffer limits, timeouts
|   |-- integrity.py              # SHA-256 hashing, StreamingHasher class
|   |-- storage.py                # StorageManager, path traversal defense, atomic replacement
|   |-- protocol.py               # Request/Response models, FramedSocket stream engine
|   |-- server.py                 # Multi-threaded TCP server implementation
|   |-- client.py                 # CFTP client with interactive REPL and CLI modes
|
|-- tests/                        # Automated unit and integration tests (90 tests)
|   |-- test_protocol.py          # Command/response parsing, quoting, errors
|   |-- test_framing.py           # Fragmented packets, coalesced buffers, split delimiters
|   |-- test_integrity.py         # SHA-256 hashing and constant-time verification
|   |-- test_regressions.py       # Transfer integrity, framing limits, concurrent downloads, lifecycle
|   |-- test_storage.py           # Path traversal, reserved device stems, atomic commits
|   |-- test_integration.py       # Full loopback TCP client-server test suite
|
|-- docs/                         # Academic documentation deliverables
|   |-- protocol-specification.md # Formal RFC-style protocol specification
|   |-- architecture.md           # System architecture, data flow, memory model
|   |-- wireshark-analysis.md     # Packet capture workflow, filters, stream inspection
|   |-- testing-report.md         # Comprehensive test report and benchmark results
|   |-- demonstration-guide.md    # 5-10 minute presentation guide and viva Q&A
|
|-- scripts/                      # Utility and execution scripts
|   |-- run_server.ps1            # Windows PowerShell server runner
|   |-- run_client.ps1            # Windows PowerShell client runner
|   |-- run_server.sh             # Linux/macOS server runner
|   |-- run_client.sh             # Linux/macOS client runner
|   |-- benchmark.py              # Automated performance and throughput benchmark
|   |-- demo_traffic.py           # Repeatable traffic generator for Wireshark
|
|-- captures/                     # Designated folder for .pcap / .pcapng files
|-- uploads/                      # Sample client upload files
|-- downloads/                    # Verified client downloads
|-- requirements.txt              # Standard library note (zero external packages)
|-- .gitignore                    # Git ignore configuration
`-- README.md                     # Main documentation entry point
```

---

## 4. Quick Start & Execution Instructions

### Prerequisites
- Python 3.10 or higher.
- No third-party packages required (uses standard library modules: `socket`, `threading`, `hashlib`, `pathlib`, `argparse`, `logging`, `unittest`).

### 4.1 Running on Windows (PowerShell)

**Terminal 1 — Start the Server:**
```powershell
python -m cftp.server --port 9000
# Or using the helper script:
.\scripts\run_server.ps1 -Port 9000
```

**Terminal 2 — Launch Client (Interactive REPL):**
```powershell
python -m cftp.client --port 9000
# Or using the helper script:
.\scripts\run_client.ps1 -Port 9000
```

Inside the interactive shell:
```text
cftp> list
cftp> put uploads/sample.txt
cftp> get sample.txt
cftp> quit
```

**Non-Interactive CLI Execution:**
```powershell
# List remote files:
python -m cftp.client list

# Upload a file:
python -m cftp.client put uploads/sample.txt

# Download a file:
python -m cftp.client get sample.txt --dest downloads/my_copy.txt
```

### 4.2 Running on Linux / macOS (Bash)

**Terminal 1 — Server:**
```bash
python3 -m cftp.server --port 9000
# Or:
./scripts/run_server.sh 127.0.0.1 9000
```

**Terminal 2 — Client:**
```bash
python3 -m cftp.client --port 9000
# Or:
./scripts/run_client.sh 127.0.0.1 9000
```

---

## 5. Running Automated Tests

Run the complete test suite using Python's standard `unittest` test runner:

```bash
python -m unittest discover -s tests -v
```

**Observed Execution Summary:**
- **90 test cases** covering framing, protocol parsing, integrity, storage containment, and live TCP loopback interaction.
- **100% Pass Rate** in ~4.3 seconds on the locally available Python 3.9.5 runtime (Windows). The documented Python 3.10+ runtime was not available for this validation.

---

## 6. Running Performance Benchmarks

CFTP includes a built-in benchmark script to evaluate throughput across file sizes from 0 bytes to 5 MB:

```bash
python scripts/benchmark.py --standalone
```

**Measured Loopback Performance:**
- **0 B (Empty File):** 5.04 ms latency, verified SHA-256 (`e3b0...`).
- **1 KB (Small Text):** 5.89 ms latency, 0.17 MB/s.
- **64 KB (Medium Buffer):** 5.66 ms latency, 11.04 MB/s.
- **1 MB (Large Binary):** 12.75 ms latency, 78.44 MB/s.
- **5 MB (Streaming Bulk):** 33.53 ms latency, 149.13 MB/s.

---

## 7. Inspecting Traffic in Wireshark

1. Open **Wireshark** and select the loopback adapter (`Adapter for loopback traffic capture` on Windows, `lo` on Linux).
2. Set display filter:
   ```text
   tcp.port == 9000
   ```
3. Start the server (`python -m cftp.server`).
4. In another shell, run the automated capture generator:
   ```bash
   python scripts/demo_traffic.py
   ```
5. In Wireshark, observe:
   - **TCP 3-Way Handshake:** `[SYN]`, `[SYN, ACK]`, `[ACK]`.
   - **CFTP Control Frames:** PSH-ACK packets containing `CFTP/1.0 LIST\r\n`.
   - **Payload Transfers:** Length-prefixed binary segments.
   - **Orderly Termination:** `[FIN, ACK]` handshakes.
   - Right click any packet -> **Follow** -> **TCP Stream** to view the reconstructed exchange.

Refer to [`docs/wireshark-analysis.md`](docs/wireshark-analysis.md) for full screenshots, filter recipes, and stream dissection notes.

---

## 8. Known Limitations & Academic Scope

1. **Unencrypted Transport (Plain TCP):** CFTP transmits control lines and payloads in plain text. In a production environment, transport security would be added by wrapping the TCP socket with TLS via Python's standard `ssl` library (`ssl.wrap_socket`).
2. **Peer Authentication:** CFTP/1.0 currently does not mandate username/password credentials or token-based authorization.
3. **Single-File Operations:** Batch wildcard operations (`mget *`, `mput *`) and recursive directory trees are intentionally omitted in CFTP/1.0 to prioritize core framing clarity and simplicity.

---

## 9. Academic Deliverables Index

- [Protocol Specification (RFC-Style)](docs/protocol-specification.md)
- [System Architecture & Data Flow](docs/architecture.md)
- [Wireshark Analysis & Capture Workflow](docs/wireshark-analysis.md)
- [Automated Testing & Verification Report](docs/testing-report.md)
- [Demonstration & Viva Presentation Guide](docs/demonstration-guide.md)
