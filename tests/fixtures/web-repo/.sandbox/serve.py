"""A tiny HTTPS app: answers every GET with the environment sandbox injected."""

import http.server
import json
import os
import ssl


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(
            {
                "user": os.getuid(),
                "WORKTREE": os.environ.get("WORKTREE"),
                "APP_URL": os.environ.get("APP_URL"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/sandbox/tls/cert.pem", "/sandbox/tls/key.pem")
server = http.server.ThreadingHTTPServer(("0.0.0.0", 443), Handler)  # noqa: S104
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
