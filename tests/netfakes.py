"""Servidores locales de mentira para probar sin red: un «CLOB» + «geoblock» y un proxy HTTP que cuenta peticiones."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakePolymarket:
    def __init__(self, blocked=False, country="ES"):
        self.blocked, self.country = blocked, country
        self.hits = []          # (method, path, headers)
        self.fail_create = False
        self.profiles = {}      # dirección firmante (minúsculas) -> proxyWallet
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, raw=False):
                body = (obj if raw else json.dumps(obj)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                outer.hits.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}))
                path = self.path.split("?")[0]
                if path == "/api/geoblock":
                    return self._send(200, {"blocked": outer.blocked, "ip": "203.0.113.9", "country": outer.country, "region": "X"})
                if path == "/time":
                    import time

                    return self._send(200, str(int(time.time() + getattr(outer, "skew", 0))), raw=True)
                if path == "/public-profile":
                    from urllib.parse import parse_qs, urlparse

                    addr = (parse_qs(urlparse(self.path).query).get("address") or [""])[0].lower()
                    if addr in outer.profiles:
                        return self._send(200, {"proxyWallet": outer.profiles[addr], "name": "demo", "pseudonym": "Demo-Demo"})
                    return self._send(404, {"type": "not found error", "error": "profile not found"})
                if path == "/auth/api-key" and method == "POST":
                    if outer.fail_create:
                        return self._send(400, {"error": "Could not create api key"})
                    return self._send(200, {"apiKey": "ak-created-1234567890", "secret": "sec-created-abcdefghij", "passphrase": "pp-created-klmnopqrst"})
                if path == "/auth/derive-api-key":
                    return self._send(200, {"apiKey": "ak-derived-1234567890", "secret": "sec-derived-abcdefghij", "passphrase": "pp-derived-klmnopqrst"})
                return self._send(404, {"error": "nf"})

            def do_GET(self):
                self._handle("GET")

            def do_HEAD(self):
                self.send_response(200)          # send_response añade la cabecera «Date» (la usa el reloj de reserva)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self._handle("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class CountingProxy:
    """Proxy HTTP mínimo (reenvía peticiones con URL absoluta) que cuenta lo que pasa por él."""

    def __init__(self):
        self.count = 0
        self.paths = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _fwd(self, method):
                outer.count += 1
                outer.paths.append(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else None
                req = urllib.request.Request(self.path, data=data, method=method)
                for k, v in self.headers.items():
                    if k.lower() not in ("proxy-connection", "host", "content-length", "connection"):
                        req.add_header(k, v)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        body, code = r.read(), r.status
                except urllib.error.HTTPError as e:
                    body, code = e.read(), e.code
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._fwd("GET")

            def do_POST(self):
                self._fwd("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
