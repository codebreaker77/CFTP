# File broadcasts

Broadcasting sends a server file to every currently connected CFTP client that
has negotiated event reception. Updated clients enable reception by default.
The sender is included. Disconnected clients are not queued for offline delivery.

## Try it

Start the web workspace:

```powershell
python -m cftp.web --demo
```

In another terminal, from the same project folder:

```powershell
python -m cftp.client --download-dir downloads/client-2 listen
```

Open http://127.0.0.1:8787, upload a file, then click its broadcast icon or choose
**Broadcast** and select the file. The Broadcasts page shows connected clients,
received files, delivery history, and each recipient's verification status.

The listening client's file is saved in `downloads/client-2/`. Keep the terminal
open until delivery finishes. Existing same-name files in a recipient's designated
download folder are replaced only after successful verification. The web workspace
keeps distinct versions in its session inbox; choose its save icon to download one.

To broadcast from the CLI:

```powershell
python -m cftp.client broadcast your-file.txt
python -m cftp.client peers
```

This uses an already uploaded server file. The broadcast command waits for the
recipient results; it exits unsuccessfully for partial or failed delivery.

## Protocol extension

1. `HELLO NAME="client name" RECEIVES=1` registers reception and returns a client ID
   plus the server's idle timeout. `RECEIVES=0` explicitly disables reception.
   Legacy raw clients that do not negotiate reception retain request/response behavior.
2. `BROADCAST <filename>` returns a length-prefixed JSON job, including its ID and
   the snapshot of eligible connected recipients. Payload transmission follows asynchronously.
3. Each recipient receives `CFTP/1.0 210 BROADCAST ID=<id> FILENAME=<name> SIZE=<n>
   CHECKSUM=<sha256>`, followed by exactly n bytes.
4. The client stages the payload, verifies SHA-256, and commits only on a match.
   It sends `ACK ID=<id> STATUS=VERIFIED CHECKSUM=<computed digest>` or `STATUS=FAILED`.
   ACK deliberately has no response frame.
5. `BSTATUS ID=<id>` returns recipient statuses; `PEERS` lists connected clients.
   `PING`/`PONG` maintains idle receiving sessions.

All ordinary request handlers and pushed frames share a per-session send lock,
keeping headers and payloads contiguous. Client operations share an I/O lock.
Broadcast ACKs are deferred until a foreground request's payload has completed,
preventing an ACK command from being mistaken for PUT file bytes.

The server takes one immutable disk snapshot and uses a shared eight-worker pool.
At most four broadcasts run concurrently. Busy clients can wait up to 30 seconds
for their send lock; missing acknowledgments fail after a ten-second ACK window.
Results distinguish queued, sent, verified, and failed; sending bytes alone is not
reported as verified delivery. Up to 100 jobs are retained in memory.

Every Python client instance is a recipient. One web bridge is one persistent
client, so browser tabs connected to that same bridge share the inbox. Start another
bridge with a different web port or run another listener to test separate clients.

The protocol still uses plain TCP without peer authentication. This extension does
not add internet-facing access control. Server broadcasts are ordinary TCP unicast.
