"""A service that listens on a port it does not declare: greets every connection on 9000."""

import socketserver


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"worker ready\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


Server(("0.0.0.0", 9000), Handler).serve_forever()  # noqa: S104
