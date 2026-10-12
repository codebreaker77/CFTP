# CFTP compared with FTP

Date: 2026-10-13. “FDP” in the improvement request is interpreted as FTP, the
protocol discussed in the preceding comparison.

CFTP has useful design advantages, but it is not yet a general-purpose replacement
for FTP. A simpler interface is a product advantage, not proof of a faster protocol.

| Area | This CFTP implementation | FTP |
| --- | --- | --- |
| Connections | Commands and file payloads share a persistent TCP connection. Broadcast events also use this stream. | Separate control and data connections. |
| Integrity | SHA-256 verification is required for successful file transfers. | Base FTP does not require a whole-file cryptographic checksum. Extensions and applications can add verification. |
| Failed uploads | Staging files are published only after checksum verification. | Atomic publication depends on the server and client workflow. |
| Simplicity | Small Python implementation, with a focused file workspace. | A broader, standardized protocol. |
| Interface | Browser UI with search, uploads, downloads, and transfer history. | Depends on the FTP application; FTP applications can provide comparable UIs. |
| Features | Flat file storage, verified transfers, and server broadcasts. | Directory operations, authentication, and standardized restart extensions. |
| Security | Plain TCP, with no peer authentication. Hashes detect corruption but do not authenticate the sender. | Plain FTP is unencrypted; FTPS adds TLS. |
| Performance | Must be measured for specific workloads. | Mature implementations can be very efficient, including optimized bulk I/O. |

The defensible project claim is: **CFTP simplifies connection management and makes
checksum verification and verified publication part of its transfer workflow.**
Broadcasts extend that workflow to all currently connected, broadcast-capable CFTP
clients. A disconnected client is not an offline delivery target. Raw legacy clients
must negotiate event support before receiving pushed data.

## Primary references

- [RFC 959: File Transfer Protocol](https://www.rfc-editor.org/rfc/rfc959.html): control/data connections, commands, and authentication.
- [RFC 3659: Extensions to FTP](https://www.rfc-editor.org/rfc/rfc3659.html): restart and directory metadata extensions.
- [RFC 4217: Securing FTP with TLS](https://www.rfc-editor.org/rfc/rfc4217.html): FTPS transport protection.
- [Python FTP client documentation](https://docs.python.org/3/library/ftplib.html): reference benchmark client.

Measured results and optimization limits belong in [performance.md](performance.md).
