# CFTP Project Demonstration & Viva Presentation Guide

**Project:** Innovative Approach to Designing and Analyzing a Custom Application Layer File Transfer Protocol over TCP  
**Target Audience:** University Reviewers, Faculty Evaluators, and Viva Examiners  
**Estimated Demonstration Duration:** 7–10 Minutes  

---

## 1. Demonstration Setup & Prerequisites

Open two terminal windows side-by-side:
- **Terminal 1 (Left):** Server Console
- **Terminal 2 (Right):** Client Interactive Console

Ensure project dependencies are available (only standard Python 3.10+ is required).

---

## 2. Minute-by-Minute Demonstration Script

### Step 1: Start the CFTP Server (1 Minute)
In **Terminal 1**, run:
```powershell
# Windows
python -m cftp.server --port 9000
```
```bash
# Linux / macOS
python3 -m cftp.server --port 9000
```

**What to point out to the examiner:**
> *"The server creates a standard TCP socket using `socket.socket(socket.AF_INET, socket.SOCK_STREAM)` and binds to port 9000. It utilizes `SO_REUSEADDR` to prevent socket binding conflicts and spawns isolated worker threads for every inbound client connection."*

---

### Step 2: Launch the Client Interactive REPL (1 Minute)
In **Terminal 2**, launch the client shell:
```powershell
python -m cftp.client --port 9000
```

**Observer Output:**
```text
================================================================
  CFTP/1.0 — Custom File Transfer Protocol Interactive Shell
  Target Server: 127.0.0.1:9000
  Type 'help' for available commands, 'quit' to exit.
================================================================
[+] Connected to 127.0.0.1:9000

cftp>
```

**What to explain:**
> *"At this exact moment, the TCP 3-way handshake (SYN, SYN-ACK, ACK) has established a persistent byte stream. Notice the server console logs the accepted connection with client IP and ephemeral source port."*

---

### Step 3: Demonstrate LIST Command (1 Minute)
In **Terminal 2**, type:
```text
cftp> list
```

**What to explain:**
> *"CFTP sends `CFTP/1.0 LIST\r\n`. Notice that unlike FTP which opens a secondary data port, CFTP uses framed length-prefixed streaming on the existing TCP connection. The server replies with `200 OK COUNT=n SIZE=bytes` and the client reads exactly that many bytes."*

---

### Step 4: Demonstrate File Upload (PUT) with SHA-256 (2 Minutes)
Create a sample document or upload one:
```powershell
# In a third shell or ahead of time:
echo "Custom Application Layer Networking Demonstration" > uploads/demo_file.txt
```
In **Terminal 2**, type:
```text
cftp> put uploads/demo_file.txt
```

**Observer Output:**
```text
[*] Uploading 'uploads/demo_file.txt'...
+--------------------------------------------------------------+
| TRANSFER RESULT: SUCCESS                                     |
+--------------------------------------------------------------+
| File:               demo_file.txt                            |
| Transferred:        54 bytes (0.00 MB)
| Duration:           0.0058 seconds
| Throughput:         0.01 MB/s
| Expected SHA-256:   d5a8c9b2...
| Actual SHA-256:     d5a8c9b2...
| Integrity Status:   MATCHED (VERIFIED)
+--------------------------------------------------------------+
```

**What to explain:**
> *"Here we demonstrate our two-phase upload protocol. The client hashes the source file locally and sends `PUT demo_file.txt SIZE=54 CHECKSUM=<hash>`. The server verifies limits and admits the upload with `200 OK READY_FOR_DATA`. As raw bytes stream into a temporary `.cftp_part` file, the server computes SHA-256 in real time. Once matched, the server atomically renames the staging file to final storage."*

---

### Step 5: Demonstrate File Download (GET) (1 Minute)
In **Terminal 2**, type:
```text
cftp> get demo_file.txt
```

**What to explain:**
> *"On download, the server sends `200 OK SIZE=54 CHECKSUM=<hash>`. The client streams the exact byte length into a temporary file in `downloads/`, computes the cryptographic digest, and atomically commits the file only upon verified integrity."*

---

### Step 6: Demonstrate Security & Fault Resistance (2 Minutes)
Demonstrate how the protocol prevents malicious behavior:

1. **Path Traversal Attack Attempt:**
   ```text
   cftp> get ../../secret.txt
   ```
   **Result:** `Server error 403 ACCESS_DENIED: Path traversal detected`
   > *"The StorageManager canonicalizes all paths and rejects directory separators and climbing syntax."*

2. **Non-Existent File:**
   ```text
   cftp> get missing_file.bin
   ```
   **Result:** `Server returned 404 NOT_FOUND`

3. **Graceful Termination:**
   ```text
   cftp> quit
   ```
   **Result:** `Server acknowledged QUIT: GOODBYE`
   > *"Demonstrates orderly 4-way TCP FIN connection teardown."*

---

### Step 7: Run Automated Tests & Benchmarks (2 Minutes)
Demonstrate code quality to the examiners:

1. **Run 90 Automated Tests:**
   ```powershell
   python -m unittest discover -s tests -v
   ```
   *(Show that framing, protocol syntax, integrity, storage security, and integration all pass in under 0.5s).*

2. **Run Performance Benchmark:**
   ```powershell
   python scripts/benchmark.py --standalone
   ```
   *(Displays real-time throughput from 0 bytes up to 5 MB streaming).*

---

## 3. High-Probability Viva Questions & Model Answers

### Q1: Why did you create CFTP instead of just using HTTP or FTP?
> **Answer:** *"HTTP and FTP are established application protocols with significant overhead and complexity (HTTP header bloat, FTP dual-connection architectures). The purpose of this academic project is to understand how application-layer protocols are designed from scratch over raw TCP, specifically solving TCP stream framing, boundary preservation, and application-level integrity verification."*

### Q2: Why is SHA-256 needed if TCP already has a 16-bit checksum?
> **Answer:** *"TCP's 16-bit checksum operates only hop-by-hop at the transport layer to catch transient transmission bit errors. It does not protect against proxy corruption, partial software buffer truncations, man-in-the-middle alterations, or filesystem write corruptions. Application-layer SHA-256 provides end-to-end cryptographic proof that the exact intended file arrived unmodified."*

### Q3: What is the 'TCP framing problem' and how did you solve it?
> **Answer:** *"TCP is a stream-oriented protocol, not message-oriented. It does not preserve application message boundaries—multiple sends can be combined into one `recv()`, or a single send can be fragmented across many `recv()` calls. We solved this with a dual-framing architecture: CRLF (`\r\n`) delimiter parsing with a dedicated unread buffer for control lines, and strict length-prefixed (`SIZE=<n>`) chunked reading for file payloads."*

### Q4: How does your implementation handle large files without running out of RAM?
> **Answer:** *"We use bounded-memory streaming (`FramedSocket.stream_exact_to_file`). It reads and writes in fixed 64 KB chunks, feeding the `StreamingHasher` incrementally. Memory usage remains constant (O(1)) regardless of whether the file is 10 KB or 10 GB."*
