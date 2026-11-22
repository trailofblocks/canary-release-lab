"""Synthetic checkout API and traffic generator with Prometheus counters."""

import argparse
import http.client
import json
import os
import re
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from time import monotonic


def valid_label(value: str | None) -> str:
    return value if value and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) else "unknown"


class Counters:
    def __init__(self):
        self._counts = Counter()
        self._lock = Lock()

    def record(self, release: str | None, revision: str | None, status: int) -> None:
        key = (valid_label(release), valid_label(revision), "error" if status >= 500 else "ok")
        with self._lock:
            self._counts[key] += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"release": release, "revision": revision, "outcome": outcome, "requests": count}
                    for (release, revision, outcome), count in sorted(self._counts.items())]

    def metrics(self) -> str:
        lines = [
            "# HELP canary_lab_requests_total Completed synthetic checkout requests by upstream revision.",
            "# TYPE canary_lab_requests_total counter",
        ]
        for row in self.snapshot():
            labels = f'release="{row["release"]}",revision="{row["revision"]}",outcome="{row["outcome"]}"'
            lines.append(f'canary_lab_requests_total{{{labels}}} {row["requests"]}')
        return "\n".join(lines) + "\n"


class Checkout:
    def __init__(self, release: str, revision: str, failure_interval: int):
        self.release = release
        self.revision = revision
        self.failure_interval = failure_interval
        self._requests = 0
        self._lock = Lock()

    def respond(self) -> int:
        with self._lock:
            self._requests += 1
            return 500 if self.failure_interval and self._requests % self.failure_interval == 0 else 200


class Handler(BaseHTTPRequestHandler):
    def send_payload(self, status: int, body: str, content_type: str = "application/json", headers=()) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


def checkout_handler(checkout: Checkout):
    class CheckoutHandler(Handler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_payload(200, '{"status":"ok"}')
            elif self.path == "/checkout":
                status = checkout.respond()
                self.send_payload(status, json.dumps({"release": checkout.release, "status": status}), headers=(
                    ("X-Lab-Release", checkout.release), ("X-Lab-Revision", checkout.revision),
                ))
            else:
                self.send_payload(404, '{"detail":"Unknown endpoint"}')

    return CheckoutHandler


def load_handler(counters: Counters):
    class LoadHandler(Handler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_payload(200, '{"status":"ok"}')
            elif self.path == "/snapshot":
                self.send_payload(200, json.dumps(counters.snapshot()))
            elif self.path == "/metrics":
                self.send_payload(200, counters.metrics(), "text/plain; version=0.0.4; charset=utf-8")
            else:
                self.send_payload(404, '{"detail":"Unknown endpoint"}')

    return LoadHandler


def generate(counters: Counters, host: str, stop: Event) -> None:
    while not stop.is_set():
        started = monotonic()
        connection = http.client.HTTPConnection(host, port=8080, timeout=0.8)
        try:
            connection.request("GET", "/checkout", headers={"Connection": "close"})
            response = connection.getresponse()
            response.read()
            counters.record(response.getheader("X-Lab-Release"), response.getheader("X-Lab-Revision"), response.status)
        except (OSError, http.client.HTTPException):
            counters.record(None, None, 503)
        finally:
            connection.close()
        stop.wait(max(0, 0.125 - (monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "load", "snapshot"))
    args = parser.parse_args()
    if args.mode == "snapshot":
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=4)
        try:
            connection.request("GET", "/snapshot")
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("Traffic snapshot unavailable.")
            print(response.read().decode())
        finally:
            connection.close()
        return
    stop = Event()
    if args.mode == "serve":
        checkout = Checkout(valid_label(os.getenv("LAB_RELEASE")), valid_label(os.getenv("LAB_REVISION")),
                            int(os.getenv("LAB_FAILURE_INTERVAL", "0")))
        handler = checkout_handler(checkout)
    else:
        counters = Counters()
        host = os.getenv("LAB_CHECKOUT_HOST", "checkout")
        for _ in range(5):
            Thread(target=generate, args=(counters, host, stop), daemon=True).start()
        handler = load_handler(counters)
    try:
        with ThreadingHTTPServer(("0.0.0.0", 8080), handler) as server:
            server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
