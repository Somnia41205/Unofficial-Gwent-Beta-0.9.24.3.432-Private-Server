#!/usr/bin/env python3
"""
Local HTTPS reverse proxy for Gwent Beta Private Server.

Listens on 127.0.0.1:443 with the self-signed cert (trusted via certutil).
Forwards all requests to the remote Oracle Cloud server over HTTPS.

This solves the Galaxy SDK TLS issue: the native SDK validates certs against
the Windows root store. By terminating TLS locally with our trusted cert,
the SDK's verification passes. The proxy then re-encrypts to the remote server.

Also handles the WebSocket upgrade for the broker connection on port 8445.
"""

import http.client
import http.server
import json
import socket
import ssl
import threading

# These get set by the launcher before starting
CERT_FILE = ""
KEY_FILE = ""
REMOTE_SERVER = ""
LISTEN_PORT = 443
BROKER_PORT = 8445


class ReverseProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTPS reverse proxy that forwards requests to the remote server."""

    # Suppress default logging
    def log_message(self, format, *args):
        pass

    def _rewrite_broker_host(self, body):
        """Rewrite the broker host in remote config responses to 127.0.0.1
        so the SDK connects to our local broker proxy instead of directly
        to the remote server."""
        try:
            data = json.loads(body)
            if "content" in data and "broker" in data["content"]:
                data["content"]["broker"]["host"] = "127.0.0.1"
                return json.dumps(data).encode()
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return body

    def _forward_request(self, method):
        """Forward an HTTP request to the remote server using http.client.
        Connects to the remote IP directly but sends the original Host header
        so nginx routes to the correct server block."""
        import http.client

        # Read request body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Connect to the remote server by IP (not by hostname, to avoid DNS loop)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(REMOTE_SERVER, 443, context=ctx, timeout=30)

        # Build headers — preserve the original Host header for nginx routing
        fwd_headers = {}
        for header, value in self.headers.items():
            header_lower = header.lower()
            # Skip hop-by-hop headers
            if header_lower in ("connection", "keep-alive", "transfer-encoding",
                                "proxy-connection"):
                continue
            # Don't send accept-encoding to avoid getting gzipped responses
            # that we'd have to decompress before rewriting
            if header_lower == "accept-encoding":
                continue
            fwd_headers[header] = value

        try:
            conn.request(method, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            # Rewrite broker host in remote config responses
            host = self.headers.get("Host", "")
            if "remote-config" in host and b"broker" in resp_body:
                resp_body = self._rewrite_broker_host(resp_body)

            self.send_response(resp.status)
            for header, value in resp.getheaders():
                header_lower = header.lower()
                if header_lower not in ("transfer-encoding", "connection",
                                        "content-length"):
                    self.send_header(header, value)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        except Exception as e:
            error_msg = f"Proxy error: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(error_msg)))
            self.end_headers()
            self.wfile.write(error_msg)
        finally:
            conn.close()

    def do_GET(self):
        self._forward_request("GET")

    def do_POST(self):
        self._forward_request("POST")

    def do_PUT(self):
        self._forward_request("PUT")

    def do_DELETE(self):
        self._forward_request("DELETE")

    def do_OPTIONS(self):
        self._forward_request("OPTIONS")

    def do_PATCH(self):
        self._forward_request("PATCH")


class ThreadedHTTPServer(http.server.HTTPServer):
    """HTTP server that handles each request in a new thread."""
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread,
                             args=(request, client_address), daemon=True)
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def _enable_keepalive(sock):
    """Enable aggressive TCP keepalive on a socket.

    The Galaxy SDK's long-lived notification WebSocket (to notifications-pusher)
    is routed through the broker proxy and stays idle for long stretches (login
    handshake, mulligan, a long think, the intentional ~20s GG delay). On Windows
    the native stack keeps idle connections warm, but under wine it does not, so
    an idle connection silently lapses and the SDK logs
    `[SessionManager] Connection lost` and signs out (clean WS close, code 1000).
    Setting SO_KEEPALIVE with low intervals keeps idle connections alive and lets
    half-open sockets be detected promptly. Invariant-safe: socket options only.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    except Exception:
        pass


def _tcp_proxy_pipe(src, dst):
    """Pipe data from src socket to dst socket."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def run_broker_proxy(cert_file, key_file, remote_server, remote_port=8445, listen_port=8445):
    """
    TCP proxy for the broker WebSocket connection.
    Listens on 127.0.0.1:8445 with TLS, forwards to remote_server:8445 without TLS
    (broker uses ws://, not wss://).

    Actually, the broker on the remote server uses plain WebSocket (no TLS).
    The SDK connects to the broker host:port from remote config.
    Since we'll set remote config to return 127.0.0.1:8445, the SDK will connect
    to us on 8445. We just need to pipe it to the remote server's 8445.

    The broker connection is plain TCP (no TLS) — the SDK only uses TLS for
    HTTPS endpoints (auth.gog.com, remote-config.gog.com, etc.), not for the
    broker WebSocket.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", listen_port))
    server_sock.listen(5)

    while True:
        try:
            client_sock, client_addr = server_sock.accept()
            _enable_keepalive(client_sock)
            # Connect to the remote broker
            remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_sock.settimeout(10)
            remote_sock.connect((remote_server, remote_port))
            remote_sock.settimeout(None)
            _enable_keepalive(remote_sock)

            # Bidirectional pipe
            t1 = threading.Thread(target=_tcp_proxy_pipe,
                                  args=(client_sock, remote_sock), daemon=True)
            t2 = threading.Thread(target=_tcp_proxy_pipe,
                                  args=(remote_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            pass


def run_relay_proxy(remote_server, remote_port=7777, listen_port=7777):
    """
    TCP proxy for the game relay (WebSocket on port 7777).
    Plain TCP passthrough — no TLS involved.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", listen_port))
    server_sock.listen(5)

    while True:
        try:
            client_sock, client_addr = server_sock.accept()
            _enable_keepalive(client_sock)
            remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_sock.settimeout(10)
            remote_sock.connect((remote_server, remote_port))
            remote_sock.settimeout(None)
            _enable_keepalive(remote_sock)

            t1 = threading.Thread(target=_tcp_proxy_pipe,
                                  args=(client_sock, remote_sock), daemon=True)
            t2 = threading.Thread(target=_tcp_proxy_pipe,
                                  args=(remote_sock, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            pass


def run_https_proxy(cert_file, key_file, remote_server, listen_port=443):
    """Start the local HTTPS reverse proxy."""
    global CERT_FILE, KEY_FILE, REMOTE_SERVER, LISTEN_PORT
    CERT_FILE = cert_file
    KEY_FILE = key_file
    REMOTE_SERVER = remote_server
    LISTEN_PORT = listen_port

    server = ThreadedHTTPServer(("127.0.0.1", listen_port), ReverseProxyHandler)

    # Wrap with TLS using our self-signed cert
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    server.serve_forever()
    return server
