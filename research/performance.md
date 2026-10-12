# CFTP performance research

Date: 2026-10-13. The objective is to reduce overhead without removing checksum
verification or atomic publication. The implementation does not claim to beat
every FTP server on every network or file size.

## Implemented improvements

1. **Persistent browser session.** The HTTP bridge reuses a CFTP connection across
   file operations. It also keeps the workspace available as a broadcast recipient.
   Previously, each HTTP action created and terminated a new CFTP connection.
2. **Verified checksum cache.** Completed uploads prime a bounded, 512-entry cache.
   GET and LIST reuse digests when descriptor identity, size, modification time,
   and change time match. Ordinary external edits and atomic replacements invalidate
   the entry. The cache assumes a locally controlled storage directory; deliberately
   falsified filesystem metadata is outside that assumption.
3. **Avoid redundant sender hashing.** PUT computes its declared digest once; the
   receiver hashes the actual streamed bytes and returns its computed digest. The
   client requires those digests to match. Hashing the sender's stream a second time
   adds no integrity guarantee when receiver verification already checks the bytes.
4. **Bulk sendfile where available.** Unhashed outbound streams use OS-supported
   `sendfile`. Windows uses the bounded chunk loop, avoiding Python's smaller
   sendfile fallback. Checksum verification remains at the receiver.
5. **Compact metadata and prompt control traffic.** JSON responses omit indentation.
   TCP_NODELAY reduces small control-message delays. The payload buffer remains
   64 KB: a 256 KB trial did not justify retaining the larger buffer.
6. **Parallel broadcasts.** One immutable file snapshot is delivered through a
   shared eight-worker pool, with at most four active broadcasts. A busy recipient
   does not prevent delivery to other clients. Verified success requires a receiver ACK.

## Benchmark method

The comparison uses Python's `ftplib` with pyftpdlib 2.2.0, passive FTP, and persistent
connections for both protocols. Both clients use the same 64 KB chunk size.
Files are random binary data. Each operation has a warmup, and execution order
alternates between trials. Timings are wall-clock medians.

CFTP timing includes SHA-256 verification and temporary-file publication. FTP's
timed interval is a raw transfer; FTP byte equality is checked afterward, outside
the interval. This deliberately favors FTP rather than manufacturing a favorable
comparison. These are implementation measurements, not proof of protocol superiority.

Local measurements ran on Windows with Python 3.9.5. There is appreciable scheduler
and filesystem jitter at millisecond scales; retain the raw samples when interpreting
small differences. The earlier 7-trial `benchmark-before-buffer.json` is an exploratory
measurement, not a controlled before/after ablation.

### Loopback: 21 trials per operation

| File | CFTP upload (ms) | FTP upload (ms) | CFTP download (ms) | FTP download (ms) |
| --- | ---: | ---: | ---: | ---: |
| 1 KB | 5.112 | 5.468 | 5.443 | 11.209 |
| 64 KB | 4.881 | 8.487 | 7.970 | 4.439 |
| 1 MB | 10.079 | 17.919 | 11.054 | 19.316 |
| 8 MB | 50.546 | 28.845 | 33.913 | 30.287 |

CFTP wins several cases, but raw FTP is faster for the 8 MB uploads and downloads,
and for 64 KB downloads in this run. Preserving integrity has CPU and filesystem
costs. [Raw results](benchmark-results.json) include every sample.

### Equal application-byte delay: 11 trials per operation

A benchmark-only relay requests **8 ms one-way propagation delay** on both CFTP
traffic and both FTP channels. Reads and timed writes use separate queues, so a
fresh propagation delay is not serially charged for every payload chunk. It does
not simulate TCP SYN latency, loss, congestion, or a bandwidth bottleneck. Windows
timer granularity can round the requested waits upward; this is a controlled
application-delay experiment, not a measurement of a real 16 ms RTT network.

| File | CFTP upload (ms) | FTP upload (ms) | CFTP download (ms) | FTP download (ms) | Upload speedup | Download speedup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 KB | 68.656 | 131.503 | 31.351 | 99.351 | 1.915× | 3.169× |
| 64 KB | 60.857 | 129.356 | 32.411 | 93.123 | 2.126× | 2.873× |
| 1 MB | 67.981 | 124.509 | 43.844 | 108.398 | 1.832× | 2.472× |
| 8 MB | 111.180 | 152.081 | 75.315 | 131.736 | 1.368× | 1.749× |

All tested delayed workloads favored CFTP. The inference is that its smaller
request/response sequence helps when latency dominates. FTP still uses a persistent
control connection, but passive data transfers require additional control exchanges.
[Raw delayed results](benchmark-latency.json) include every sample.

## Reproduce

```powershell
python -m pip install pyftpdlib
python scripts/compare_ftp.py --trials 21 --output research/benchmark-results.json
python scripts/compare_ftp.py --trials 11 --latency-ms 8 --output research/benchmark-latency.json
```

pyftpdlib is only needed for this benchmark. Application runtime remains standard
library only. Avoid running browser tests or other CPU-intensive tasks alongside it.

## Further opportunities

- Batch small files and pipeline requests to amortize round trips; requires bounded
  queues and careful framing, not just more worker threads.
- Add a metadata-only listing option so the first LIST need not hash an externally
  populated directory. Require the digest only when a transfer starts.
- Explore a negotiated checksum trailer for single-pass upload hashing. The receiver
  must still stage and verify before publishing.
- Offer optional compression only for compressible content when saved network time
  exceeds CPU cost. Random binaries should bypass it.
- Consider resumable transfers with chunk hashes for interrupted large files. FTP
  already has restart extensions, so resume alone is not a unique CFTP advantage.
- Repeat measurements across operating systems, real LAN/WAN RTTs, file sizes,
  filesystem types, and several FTP implementations before making broader claims.

Broadcast is parallel TCP unicast: a file of size S delivered to N clients still
requires approximately N × S payload bytes. It is not UDP or IP multicast.

## Primary references

- [Python socket sendfile](https://docs.python.org/3/library/socket.html#socket.socket.sendfile)
- [Python FTP client](https://docs.python.org/3/library/ftplib.html)
- [pyftpdlib server documentation](https://pyftpdlib.readthedocs.io/en/latest/tutorial.html)
- [FTP protocol, RFC 959](https://www.rfc-editor.org/rfc/rfc959.html)
