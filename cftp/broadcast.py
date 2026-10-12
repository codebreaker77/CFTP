"""Bounded, verified server broadcasts over negotiated CFTP sessions."""

import json
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cftp.config import Config
from cftp.integrity import StreamingHasher
from cftp.protocol import ProtocolError, Response


@dataclass
class ClientSession:
    framed: object
    address: tuple
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Legacy client"
    receives: bool = False
    send_lock: object = field(default_factory=threading.RLock)


class BroadcastManager:
    """Snapshot once, send in parallel, and count success only after receiver ACK."""

    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.jobs = {}
        self.slots = threading.BoundedSemaphore(4)
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="Broadcast")
        self.stopping = threading.Event()
        self.threads = set()

    def peers(self):
        with self.server._lock:
            sessions = list(self.server._sessions.values())
        return [{"id": item.client_id, "name": item.name, "address": f"{item.address[0]}:{item.address[1]}",
                 "receives_broadcasts": item.receives} for item in sessions]

    def start(self, filename):
        source_path = self.server.storage.resolve_safe_path(filename)
        if not source_path.is_file():
            raise ProtocolError("Broadcast file not found", code=404)
        if source_path.stat().st_size > self.server.storage.max_file_size:
            raise ProtocolError("Broadcast exceeds the file size limit", code=413)
        if not self.slots.acquire(blocking=False):
            raise ProtocolError("Four broadcasts are already in progress; retry shortly", code=409)
        directory = tempfile.TemporaryDirectory(prefix="cftp-broadcast-")
        try:
            snapshot = Path(directory.name) / "payload"
            hasher = StreamingHasher()
            with source_path.open("rb") as source, snapshot.open("wb") as target:
                while chunk := source.read(Config.BUFFER_SIZE):
                    if hasher.bytes_processed + len(chunk) > self.server.storage.max_file_size:
                        raise ProtocolError("Broadcast exceeds the file size limit", code=413)
                    target.write(chunk)
                    hasher.update(chunk)
            with self.server._lock:
                recipients = [item for item in self.server._sessions.values() if item.receives]
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "filename": source_path.name, "size": hasher.bytes_processed,
                   "checksum": hasher.hexdigest(), "status": "sending", "completed_at": None,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "recipients": [{"id": item.client_id, "name": item.name, "status": "queued", "error": None} for item in recipients]}
            with self.lock:
                self.jobs[job_id] = job
                for old_id in list(self.jobs):
                    if len(self.jobs) <= 100:
                        break
                    if self.jobs[old_id]["status"] != "sending":
                        del self.jobs[old_id]
            worker = threading.Thread(target=self._run, args=(job, recipients, snapshot, directory), daemon=True)
            with self.lock:
                self.threads.add(worker)
                worker.start()
            return self.status(job_id)
        except Exception:
            directory.cleanup()
            self.slots.release()
            raise

    def _run(self, job, recipients, snapshot, directory):
        try:
            futures = [self.pool.submit(self._send, job, recipient, snapshot) for recipient in recipients]
            for future in futures:
                future.result()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.stopping.is_set():
                with self.lock:
                    if all(item["status"] in ("verified", "failed") for item in job["recipients"]):
                        break
                self.stopping.wait(0.05)
            with self.lock:
                for item in job["recipients"]:
                    if item["status"] not in ("verified", "failed"):
                        item.update(status="failed", error="Receiver did not acknowledge verification")
                verified = sum(item["status"] == "verified" for item in job["recipients"])
                job["status"] = "completed" if verified == len(recipients) else "partial" if verified else "failed"
                job["completed_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as err:
            with self.lock:
                job["status"] = "failed"
                for item in job["recipients"]:
                    if item["status"] != "verified":
                        item.update(status="failed", error=str(err))
        finally:
            directory.cleanup()
            self.slots.release()
            with self.lock:
                self.threads.discard(threading.current_thread())

    def _send(self, job, recipient, snapshot):
        if not recipient.send_lock.acquire(timeout=30):
            self._recipient(job, recipient.client_id, "failed", "Client remained busy")
            return
        try:
            if self.stopping.is_set():
                raise ConnectionError("Server stopped")
            header = Response(code=210, reason="BROADCAST", params={"ID": job["id"],
                "FILENAME": job["filename"], "SIZE": str(job["size"]), "CHECKSUM": job["checksum"]})
            # ACK may arrive immediately; mark sent before writing the frame.
            self._recipient(job, recipient.client_id, "sent")
            recipient.framed.send_line(header.to_wire())
            with snapshot.open("rb") as source:
                recipient.framed.stream_file_to_socket(source, job["size"])
        except Exception as err:
            self._recipient(job, recipient.client_id, "failed", str(err))
            # An interrupted pushed payload invalidates the stream boundary.
            recipient.framed.close()
        finally:
            recipient.send_lock.release()

    def _recipient(self, job, client_id, status, error=None):
        with self.lock:
            for item in job["recipients"]:
                if item["id"] == client_id and item["status"] != "verified":
                    item.update(status=status, error=error)
                    return

    def acknowledge(self, session, params):
        with self.lock:
            job = self.jobs.get(params.get("ID"))
            if not job or job["status"] != "sending":
                return
            valid = params.get("STATUS") == "VERIFIED" and params.get("CHECKSUM", "").lower() == job["checksum"]
            self._recipient(job, session.client_id, "verified" if valid else "failed", None if valid else "Receiver checksum verification failed")

    def status(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ProtocolError("Broadcast not found", code=404)
            result = json.loads(json.dumps(job))
        result["total"] = len(result["recipients"])
        result["verified"] = sum(item["status"] == "verified" for item in result["recipients"])
        result["failed"] = sum(item["status"] == "failed" for item in result["recipients"])
        return result

    def stop(self):
        self.stopping.set()
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self.pool.shutdown(wait=False, cancel_futures=True)
