"""Reproducible local CFTP vs FTP benchmark; pyftpdlib is benchmark-only.

Install: python -m pip install pyftpdlib
Run: python scripts/compare_ftp.py --trials 7 --output research/benchmark-results.json
"""

import argparse
import ftplib
import hashlib
from importlib.metadata import version
import json
import logging
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cftp.client import CFTPClient
from cftp.config import Config
from cftp.server import CFTPServer


def run(trials, latency_ms=0):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
    from pyftpdlib.log import config_logging
    config_logging(level=logging.ERROR)
    logging.getLogger("cftp").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ftp_root = root / "ftp"
        ftp_root.mkdir()
        tcp = CFTPServer(port=0, storage_dir=root / "cftp")
        tcp_worker = threading.Thread(target=tcp.start, daemon=True)
        tcp_worker.start()
        deadline = time.monotonic() + 5
        while not tcp._is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        authorizer = DummyAuthorizer()
        authorizer.add_user("benchmark", "local-only", str(ftp_root), perm="elradfmwMT")
        handler = type("BenchmarkFTPHandler", (FTPHandler,), {})
        handler.authorizer = authorizer
        ftp_server = FTPServer(("127.0.0.1", 0), handler)
        ftp_worker = threading.Thread(target=ftp_server.serve_forever,
            kwargs={"timeout": 0.05, "blocking": True, "handle_exit": False}, daemon=True)
        ftp_worker.start()
        proxies = []
        ftp_address = ("127.0.0.1", ftp_server.socket.getsockname()[1])
        cftp_address = ("127.0.0.1", tcp.port)
        ftp_type = ftplib.FTP
        if latency_ms:
            from latency_proxy import LatencyProxy
            cftp_proxy = LatencyProxy(cftp_address, latency_ms)
            control_proxy = LatencyProxy(ftp_address, latency_ms)
            data_proxy = LatencyProxy(ftp_address, latency_ms)
            proxies = [cftp_proxy, control_proxy, data_proxy]
            cftp_address, ftp_address = cftp_proxy.address, control_proxy.address
            class DelayedFTP(ftplib.FTP):
                def makepasv(self):
                    data_proxy.server.destination = super().makepasv()
                    return data_proxy.address
            ftp_type = DelayedFTP
        cftp = CFTPClient(host=cftp_address[0], port=cftp_address[1], download_dir=root / "downloads", receive_broadcasts=False)
        ftp = ftp_type()
        results = []
        try:
            cftp.connect()
            ftp.connect(*ftp_address, timeout=30)
            ftp.login("benchmark", "local-only")
            ftp.set_pasv(True)
            for size in (1024, 64 * 1024, 1024 * 1024, 8 * 1024 * 1024):
                source = root / f"data-{size}.bin"
                source.write_bytes(os.urandom(size))
                name = source.name
                expected = hashlib.sha256(source.read_bytes()).hexdigest()
                times = {"cftp_put": [], "ftp_put": [], "cftp_get": [], "ftp_get": []}
                def put_cftp():
                    result = cftp.put_file(source)
                    if not result.success:
                        raise RuntimeError(result.error_message)
                def put_ftp():
                    with source.open("rb") as stream:
                        ftp.storbinary("STOR " + name, stream, blocksize=Config.BUFFER_SIZE)
                def get_cftp():
                    result = cftp.get_file(name)
                    if not result.success:
                        raise RuntimeError(result.error_message)
                def get_ftp():
                    with (root / "ftp-download.bin").open("wb") as stream:
                        ftp.retrbinary("RETR " + name, stream.write, blocksize=Config.BUFFER_SIZE)
                operations = {"cftp_put": put_cftp, "ftp_put": put_ftp, "cftp_get": get_cftp, "ftp_get": get_ftp}
                # Equal warmups; persistent sessions for both; alternate order.
                for operation in operations.values():
                    operation()
                for trial in range(trials):
                    order = list(operations) if trial % 2 == 0 else list(operations)[::-1]
                    for label in order:
                        started = time.perf_counter()
                        operations[label]()
                        times[label].append(time.perf_counter() - started)
                        # Validate FTP bytes outside its timed raw-transfer interval.
                        if label.startswith("ftp"):
                            path = ftp_root / name if label.endswith("put") else root / "ftp-download.bin"
                            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                                raise RuntimeError("FTP benchmark content mismatch")
                row = {"bytes": size, "trials": trials,
                       "median_ms": {key: round(statistics.median(values) * 1000, 3) for key, values in times.items()},
                       "samples_ms": {key: [round(value * 1000, 3) for value in values] for key, values in times.items()}}
                row["cftp_speedup"] = {
                    operation: round(row["median_ms"]["ftp_" + operation] / row["median_ms"]["cftp_" + operation], 3)
                    for operation in ("put", "get")}
                results.append(row)
                print(json.dumps({key: value for key, value in row.items() if key != "samples_ms"}), flush=True)
        finally:
            cftp.quit()
            try:
                ftp.quit()
            except OSError:
                ftp.close()
            ftp_server.close_all()
            ftp_worker.join(timeout=2)
            tcp.stop()
            tcp_worker.join(timeout=2)
            for proxy in proxies:
                proxy.close()
        return {"date": "2026-10-13", "python": platform.python_version(), "platform": platform.platform(),
            "transport": "IPv4 loopback, plain TCP, passive FTP, warm filesystem cache",
            "simulated_one_way_application_delay_ms": latency_ms,
            "ftp_server": "pyftpdlib", "ftp_server_version": version("pyftpdlib"), "client_buffer_bytes": Config.BUFFER_SIZE,
            "methodology": "Persistent sessions; one warmup per operation; alternating protocol order. CFTP includes SHA-256 verification and atomic staging. FTP intervals measure raw transfers; its byte validation is outside timing. This favors FTP and is not a universal network benchmark.",
            "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--output", default="research/benchmark-results.json")
    parser.add_argument("--latency-ms", type=float, default=0, help="Equal one-way byte propagation delay on all CFTP and FTP channels")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")
    if not 0 <= args.latency_ms <= 100:
        parser.error("--latency-ms must be between 0 and 100")
    report = run(args.trials, args.latency_ms)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
