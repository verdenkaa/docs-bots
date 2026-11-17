"""nginx with unattended first issuance and renewal of domain or IPv4 certificates."""

import hashlib
import ipaddress
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent
CERTIFICATE = Path("/etc/letsencrypt/live/rag-server/fullchain.pem")
CONFIG = Path("/etc/nginx/nginx.conf")


def identifier(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("Invalid PUBLIC_HOST") from None
        return ["--domain", host]
    if address.version != 4 or not address.is_global:
        raise ValueError("A public IPv4 address is required")
    return ["--ip-address", str(address), "--required-profile", "shortlived"]


def issue_command(values):
    command = ["certbot", "certonly", "--non-interactive", "--agree-tos", "--webroot",
               "--webroot-path", "/var/www/acme", "--cert-name", "rag-server",
               "--server", values.get("ACME_DIRECTORY_URL", "https://acme-v02.api.letsencrypt.org/directory")]
    command += identifier(values["PUBLIC_HOST"])
    email = values.get("ACME_EMAIL", "")
    command += ["--email", email] if email else ["--register-unsafely-without-email"]
    return command


def render(template, values):
    host = values["PUBLIC_HOST"]
    identifier(host)
    result = template.replace("@PUBLIC_HOST@", host)
    locations = "location / { proxy_pass $core; }"
    if values.get("MINI_APP_PROXY_ENABLED", "false").lower() == "true":
        locations = """set $mini_backend http://mini-backend:8080;
        set $mini_frontend http://mini-frontend:80;
        location /api/v1/ { proxy_pass $mini_backend; }
        location /sources/ { proxy_pass $mini_backend; }
        location /health/ { proxy_pass $core; }
        location /admin/ { proxy_pass $core; }
        location /tickets/ { proxy_pass $core; }
        location / { proxy_pass $mini_frontend; }"""
    result = result.replace("@APP_LOCATIONS@", locations)
    for token, key, default in (("UPLOAD_BYTES", "RAG_INGESTION_MAX_UPLOAD_BYTES", 20971520),
                                ("UPLOAD_TIMEOUT", "RAG_INGESTION_UPLOAD_TIMEOUT_S", 600),
                                ("PROXY_TIMEOUT", "INGESTION_PROXY_TIMEOUT_S", 660)):
        value = float(values.get(key, default))
        if value <= 0 or not value.is_integer():
            raise ValueError(key + " must be a positive integer")
        result = result.replace("@" + token + "@", str(int(value)))
    return result


def certificate_matches(host):
    option = "-checkip" if identifier(host)[0] == "--ip-address" else "-checkhost"
    if not CERTIFICATE.is_file():
        return False
    expires = subprocess.run(["openssl", "x509", "-in", str(CERTIFICATE), "-noout",
                              "-checkend", "3600"], capture_output=True)
    # OpenSSL's -checkend exits early: combining it with -checkip/-checkhost
    # silently skips checking the identity. Perform both checks separately.
    identity = subprocess.run(["openssl", "x509", "-in", str(CERTIFICATE), "-noout",
                               option, host], capture_output=True)
    return (expires.returncode == 0 and identity.returncode == 0
            and b"does match certificate" in identity.stdout)


def health(host):
    try:
        context = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1", 443), timeout=3) as connection:
            with context.wrap_socket(connection, server_hostname=host) as secure:
                secure.sendall(("GET /proxy-health HTTP/1.0\r\nHost: " + host + "\r\n\r\n").encode("ascii"))
                return secure.recv(128).split(b"\r\n", 1)[0].split()[1] == b"200"
    except (OSError, ValueError, IndexError):
        return False


def _execute(command, stop, timeout=240):
    process = subprocess.Popen(command)
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if stop.wait(0.5) or time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return False
        return process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def serve(values):
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    host = values["PUBLIC_HOST"]
    identifier(host)
    enabled = certificate_matches(host)

    def configure(tls):
        template = ROOT / ("https.conf" if tls else "bootstrap.conf")
        CONFIG.write_text(render(template.read_text(), values), encoding="utf-8")
        subprocess.run(["nginx", "-t"], check=True)

    configure(enabled)
    nginx = subprocess.Popen(["nginx", "-g", "daemon off;"])
    failures = []

    def certificates():
        nonlocal enabled
        try:
            while not stop.is_set():
                before = hashlib.sha256(CERTIFICATE.read_bytes()).digest() if CERTIFICATE.exists() else None
                if enabled:
                    success = _execute(["certbot", "renew", "--non-interactive", "--cert-name", "rag-server"], stop)
                else:
                    success = _execute(issue_command(values), stop)
                if stop.is_set():
                    return
                if success and certificate_matches(host):
                    after = hashlib.sha256(CERTIFICATE.read_bytes()).digest()
                    if not enabled or before != after:
                        configure(True)
                        subprocess.run(["nginx", "-s", "reload"], check=True)
                    enabled = True
                    print("HTTPS certificate ready for " + host, flush=True)
                    # Six-day IP certificates are checked at least four times a day.
                    stop.wait(21600)
                else:
                    print("Certificate issuance/renewal failed; retrying in five minutes. Check inbound port 80.",
                          file=sys.stderr, flush=True)
                    stop.wait(300)
        except Exception as error:
            failures.append(type(error).__name__)
            stop.set()

    renewal = threading.Thread(target=certificates, daemon=True)
    renewal.start()
    result = 0
    try:
        while not stop.wait(1):
            if nginx.poll() is not None:
                result = 1
                break
        if failures:
            print("Certificate supervisor stopped: " + failures[0], file=sys.stderr, flush=True)
            result = 1
    finally:
        stop.set()
        renewal.join(timeout=20)
        if nginx.poll() is None:
            nginx.send_signal(signal.SIGQUIT)
            try:
                nginx.wait(timeout=20)
            except subprocess.TimeoutExpired:
                nginx.kill()
                nginx.wait(timeout=5)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "health":
        sys.exit(0 if health(os.environ.get("PUBLIC_HOST", "")) else 1)
    sys.exit(serve(dict(os.environ)))
