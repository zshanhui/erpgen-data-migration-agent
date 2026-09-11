"""Offline stand-in for the DeepSeek API — test the agent loop without a key.

Usage:
    python3 scripts/mock-llm.py 8765 &            # serve on 127.0.0.1:8765
    DEEPSEEK_API_KEY=dummy .venv/bin/python scripts/agent.py \
        --run mock-01 --source samples/customers_e2e.csv --doctype Customer \
        --provider deepseek --api-base http://127.0.0.1:8765/v1

Minimal OpenAI-compatible /chat/completions mock (streaming + non-streaming).

Turn 1: asks for a tool call (describe_doctype) so the tool path + transcript run.
Turn 2+: returns a final assistant message so the loop can end.

Supports `stream: true` (SSE), which llama-index's FunctionAgent uses.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TURNS = {"n": 0}


def _chunk(delta, finish=None):
    return {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0,
            "model": "mock",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "mock", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            req = {}
        streaming = bool(req.get("stream"))
        has_tools = bool(req.get("tools"))
        TURNS["n"] += 1
        first = has_tools and TURNS["n"] == 1
        print(f"[mock] call #{TURNS['n']} stream={streaming} tools={has_tools} "
              f"-> {'tool_call' if first else 'text'}", file=sys.stderr, flush=True)

        if first:
            call = {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "describe_doctype",
                                 "arguments": json.dumps({"doctype": "Customer"})}}
            if streaming:
                self._sse([
                    _chunk({"role": "assistant", "content": ""}),
                    _chunk({"tool_calls": [call]}),
                    _chunk({}, finish="tool_calls"),
                ])
            else:
                self._json({
                    "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
                    "model": "mock",
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant", "content": None,
                                             "tool_calls": [call]}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15},
                })
            return

        text = "Checked the metadata; nothing further is needed."
        if streaming:
            self._sse([
                _chunk({"role": "assistant", "content": text}),
                _chunk({}, finish="stop"),
            ])
        else:
            self._json({
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15},
            })

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
