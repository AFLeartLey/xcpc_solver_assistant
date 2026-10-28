"""用本地 HTTP 桩服务验证 ``core.llm`` 的真实 OpenAI 协议行为（无需 API Key）。

覆盖：system prompt 前置、tools 透传、tool_calls 归一化、SSE 流式聚合、
429 指数退避重试、usage 统计。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import llm  # noqa: E402


def ok_response(content: str, *, tool_calls=None, usage=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop" if not tool_calls else "tool_calls"}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


def chunk(delta: dict, finish_reason=None, usage=None) -> dict:
    payload = {
        "id": "chunk-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage:
        payload["usage"] = usage
    return payload


class StubState:
    def __init__(self):
        self.responses: list[tuple[int, object]] = []
        self.requests: list[dict] = []
        self.paths: list[str] = []


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    state = StubState()

    def log_message(self, *args):  # noqa: A003 - 静默测试日志
        return

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            request = {}
        type(self).state.requests.append(request)
        type(self).state.paths.append(self.path)

        status, payload = type(self).state.responses.pop(0)
        if status != 200:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for item in payload:  # type: ignore[union-attr]
                self.wfile.write(b"data: " + json.dumps(item).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class LlmHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        StubHandler.state = StubState()
        cls.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        llm.configure(api_key="test-key", base_url=cls.base_url, model="stub-model")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        llm.reset_settings()

    def setUp(self):
        StubHandler.state = StubState()
        llm.configure(api_key="test-key", base_url=self.base_url, model="stub-model")

    # ------------------------------------------------------------------ #
    def test_system_prompt_injected_first(self):
        StubHandler.state.responses = [(200, ok_response("pong"))]
        result = llm.call_llm(
            [{"role": "user", "content": "ping"}], system_prompt="你是教练", stream=False
        )
        request = StubHandler.state.requests[0]
        self.assertEqual(request["messages"][0], {"role": "system", "content": "你是教练"})
        self.assertEqual(request["messages"][1], {"role": "user", "content": "ping"})
        self.assertEqual(request["model"], "stub-model")
        self.assertEqual(result["content"], "pong")
        self.assertEqual(result["usage"]["total_tokens"], 14)
        self.assertIsNone(result["tool_calls"])
        self.assertTrue(StubHandler.state.paths[0].endswith("/chat/completions"))

    def test_system_prompt_not_duplicated(self):
        StubHandler.state.responses = [(200, ok_response("ok"))]
        llm.call_llm(
            [{"role": "system", "content": "已有"}, {"role": "user", "content": "hi"}],
            system_prompt="另一个",
            stream=False,
        )
        messages = StubHandler.state.requests[0]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "已有")

    def test_tools_passed_and_tool_calls_normalized(self):
        StubHandler.state.responses = [
            (
                200,
                ok_response(
                    "",
                    tool_calls=[
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"file_path": "a.cpp"}'},
                        }
                    ],
                ),
            )
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "read file",
                    "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
                },
            }
        ]
        result = llm.call_llm([{"role": "user", "content": "read"}], tools=tools, stream=False)
        self.assertEqual(StubHandler.state.requests[0]["tools"][0]["function"]["name"], "read")
        self.assertEqual(StubHandler.state.requests[0]["tool_choice"], "auto")
        call = result["tool_calls"][0]
        self.assertEqual(call["id"], "call_9")
        self.assertEqual(call["function"]["name"], "read")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"file_path": "a.cpp"})

    def test_streaming_accumulates_content_and_tool_calls(self):
        StubHandler.state.responses = [
            (
                200,
                [
                    chunk({"role": "assistant", "content": "你"}),
                    chunk({"content": "好"}),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_s",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file'},
                                }
                            ]
                        }
                    ),
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '_path":"a.txt"}'}}]}),
                    chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}),
                ],
            )
        ]
        deltas: list[str] = []
        result = llm.call_llm(
            [{"role": "user", "content": "hi"}], on_delta=deltas.append, stream=True
        )
        request = StubHandler.state.requests[0]
        self.assertTrue(request["stream"])
        self.assertEqual(request["stream_options"], {"include_usage": True})
        self.assertEqual(deltas, ["你", "好"])
        self.assertEqual(result["content"], "你好")
        call = result["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "read")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"file_path": "a.txt"})
        self.assertEqual(result["usage"]["total_tokens"], 13)

    def test_retry_on_rate_limit(self):
        StubHandler.state.responses = [
            (429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}),
            (200, ok_response("终于成功")),
        ]
        with patch("core.llm.time.sleep", lambda _: None):
            result = llm.call_llm([{"role": "user", "content": "hi"}], stream=False)
        self.assertEqual(result["content"], "终于成功")
        self.assertEqual(len(StubHandler.state.requests), 2)

    def test_usage_estimated_when_missing(self):
        payload = ok_response("x")
        payload.pop("usage")
        StubHandler.state.responses = [(200, payload)]
        result = llm.call_llm([{"role": "user", "content": "hi"}], stream=False)
        self.assertGreater(result["usage"]["total_tokens"], 0)
        self.assertGreater(result["usage"]["prompt_tokens"], 0)

    def test_parse_json_lenient(self):
        self.assertEqual(llm.parse_json_lenient('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json_lenient('好的，结果是 {"summary": "x"} 谢谢'), {"summary": "x"})
        self.assertIsNone(llm.parse_json_lenient("没有 json"))
        self.assertEqual(llm.parse_json_lenient("[1, 2]"), {"items": [1, 2]})


if __name__ == "__main__":
    unittest.main()
