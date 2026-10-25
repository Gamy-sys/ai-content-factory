import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self) -> None:
        self.queue: list[object] = []
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0

    def push(self, item: object) -> None:
        self.queue.append(item)

    def start(self) -> "FakeOllama":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/api/tags":
                    self._send(404, {"error": "not found"})
                    return
                self._send(200, {"models": [{"name": "qwen2.5:7b"}]})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    owner.requests.append(json.loads(raw.decode()))
                except json.JSONDecodeError:
                    owner.requests.append({"_raw": raw.decode(errors="replace")})
                if self.path.split("?", 1)[0] != "/api/chat":
                    self._send(404, {"error": "not found"})
                    return
                item = owner.queue.pop(0) if owner.queue else {"error": "empty"}
                if isinstance(item, dict) and "__http__" in item:
                    self._send(int(item["__http__"]), item.get("__body__", {}))
                    return
                if isinstance(item, str):
                    content = item
                else:
                    content = json.dumps(item, ensure_ascii=False)
                self._send(
                    200,
                    {"model": "fake", "message": {"role": "assistant", "content": content}, "done": True},
                )

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}"
