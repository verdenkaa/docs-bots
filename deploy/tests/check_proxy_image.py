"""Exercise the actual nginx image with synthetic TLS and two local upstreams.

Run only inside a disposable proxy container. No ACME or public server is contacted.
"""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import threading
import time



def main():
    spec = importlib.util.spec_from_file_location("proxy", "/opt/proxy/main.py")
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    host = "176.53.246.245"
    certificate = proxy.CERTIFICATE
    certificate.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=" + host, "-addext", "subjectAltName=IP:" + host,
                    "-keyout", str(certificate.parent / "privkey.pem"), "-out", str(certificate)],
                   check=True, capture_output=True)
    assert proxy.certificate_matches(host)
    assert not proxy.certificate_matches("176.53.246.244")


    class Upstream(BaseHTTPRequestHandler):
        def respond(self):
            content = self.rfile.read(int(self.headers.get("content-length", "0")))
            body = json.dumps({"port": self.server.server_port, "path": self.path,
                               "bytes": len(content), "origin": self.headers.get("origin"),
                               "authorization": self.headers.get("authorization"),
                               "telegram_secret": self.headers.get("X-Telegram-Bot-Api-Secret-Token"),
                               "scheme": self.headers.get("X-Forwarded-Proto")}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = respond

        def log_message(self, *args):
            pass


    upstreams = [ThreadingHTTPServer(("127.0.0.1", port), Upstream) for port in (18000, 18001)]
    for upstream in upstreams:
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
    configuration = proxy.render((proxy.ROOT / "https.conf").read_text(), {"PUBLIC_HOST": host})
    configuration = configuration.replace("http://core:8000", "http://127.0.0.1:18000")
    configuration = configuration.replace("http://worker:8001", "http://127.0.0.1:18001")
    proxy.CONFIG.write_text(configuration)
    subprocess.run(["nginx", "-t"], check=True)
    nginx = subprocess.Popen(["nginx", "-g", "daemon off;"])


    def connection(context):
        client = http.client.HTTPSConnection(host, 443, context=context, timeout=10)
        # Connect locally while retaining the public IP as TLS peer identity.
        client._create_connection = lambda *args, **kwargs: socket.create_connection(("127.0.0.1", 443), timeout=10)
        return client


    try:
        context = ssl.create_default_context(cafile=str(certificate))
        for _ in range(30):
            try:
                client = connection(context)
                client.request("GET", "/proxy-health")
                assert client.getresponse().status == 200
                client.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("nginx did not start")
        assert not proxy.health(host), "Untrusted self-signed TLS must fail the production healthcheck"
        os.environ["SSL_CERT_FILE"] = str(certificate)
        assert proxy.health(host)
        client = connection(context)
        client.request("POST", "/telegram/webhook", body=b"{}", headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic"})
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200 and payload["port"] == 18000
        assert payload["telegram_secret"] == "synthetic" and payload["scheme"] == "https"
        client.close()
        client = connection(context)
        client.request("POST", "/admin/ingestion/upload?filename=test.md&access_level=employees", body=b"a" * (2 * 1024 * 1024),
                       headers={"Authorization": "Bearer synthetic", "Origin": "https://" + host})
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200 and payload["port"] == 18001
        assert payload["bytes"] == 2 * 1024 * 1024 and "filename=test.md" in payload["path"]
        assert payload["authorization"] == "Bearer synthetic" and payload["origin"] == "https://" + host
        client.close()
        client = connection(context)
        client.putrequest("POST", "/admin/ingestion/upload?filename=large.md&access_level=employees")
        client.putheader("Content-Length", "20971521")
        client.endheaders()
        assert client.getresponse().status == 413
        client.close()
        client = connection(context)
        client.request("GET", "/openapi.json")
        assert client.getresponse().status == 404
        client.close()
        challenge = Path("/var/www/acme/.well-known/acme-challenge/probe")
        challenge.parent.mkdir(parents=True, exist_ok=True)
        challenge.write_text("synthetic-challenge")
        client = http.client.HTTPConnection("127.0.0.1", 80, timeout=5)
        client.request("GET", "/.well-known/acme-challenge/probe")
        assert client.getresponse().read() == b"synthetic-challenge"
        client.close()
        client = http.client.HTTPConnection("127.0.0.1", 80, timeout=5)
        client.request("GET", "/admin", headers={"Host": "untrusted.example"})
        response = client.getresponse()
        assert response.status == 308 and response.getheader("Location") == "https://" + host + "/admin"
        client.close()
        print("Proxy image passed: TLS trust/IP SAN, ACME, redirect, upstream routes, headers, 2 MiB upload and 413 limit")
    finally:
        nginx.send_signal(signal.SIGQUIT)
        nginx.wait(timeout=20)
        for upstream in upstreams:
            upstream.shutdown()


if __name__ == "__main__":
    main()
