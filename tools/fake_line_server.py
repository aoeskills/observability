"""Stand-in for the LINE Messaging API push endpoint, for testing alerts without sending real messages.

    docker run --rm -d --name fake-line --network observability_default \
      -v "$PWD/tools/fake_line_server.py:/s.py:ro" python:3-alpine python3 -u /s.py
    LINE_PUSH_URL=http://fake-line:8080/v2/bot/message/push docker compose -f docker-compose.local.yml up -d --wait provisioner
    docker compose -f docker-compose.local.yml restart grafana     # Grafana reads alert rules only on start
    docker logs -f fake-line
    # afterwards: the same two commands without LINE_PUSH_URL

Logs every request: whether an Authorization header was sent (and a sha256 prefix of the token), whether the body is valid JSON, and the text of each
message. Answers 200 like LINE does. The token itself is never printed.
"""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        # sha256 prefix only: enough to tell which credential was sent, without printing it
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:8] if token else "none"
        print(f"--- {self.path}  auth={'Bearer' if token else repr(auth)} token-sha256:{fingerprint}")
        try:
            payload = json.loads(body)
            print(f"to={payload.get('to')!r}")
            for message in payload.get("messages", []):
                print(message.get("text", message))
        except ValueError as err:
            print(f"INVALID JSON ({err}):\n{body}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
