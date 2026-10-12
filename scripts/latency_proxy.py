"""Benchmark-only application-byte relay with equal delay in both directions.

This models propagation delay, not TCP SYN latency, loss, or bandwidth limits.
Reader and writer queues prevent charging the delay again for every chunk.
"""

import queue
import socket
import socketserver
import threading
import time


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            remote = socket.create_connection(self.server.destination, timeout=30)
            remote.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            worker = threading.Thread(target=self.pipe, args=(self.request, remote), daemon=True)
            worker.start()
            self.pipe(remote, self.request)
            worker.join(timeout=2)
            remote.close()
        except OSError:
            pass

    def pipe(self, source, target):
        pending = queue.Queue(maxsize=64)
        stop = threading.Event()
        def read():
            try:
                while not stop.is_set():
                    chunk = source.recv(65536)
                    pending.put((time.monotonic() + self.server.delay, chunk), timeout=30)
                    if not chunk:
                        break
            except (OSError, queue.Full):
                try:
                    pending.put((time.monotonic(), b""), timeout=1)
                except queue.Full:
                    pass
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while True:
                due, chunk = pending.get(timeout=30)
                stop.wait(max(0, due - time.monotonic()))
                if not chunk:
                    target.shutdown(socket.SHUT_WR)
                    break
                target.sendall(chunk)
        except (OSError, queue.Empty):
            pass
        finally:
            stop.set()


class LatencyProxy:
    def __init__(self, destination, delay_ms):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Relay)
        self.server.daemon_threads = True
        self.server.destination = destination
        self.server.delay = delay_ms / 1000
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.address = self.server.server_address

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
