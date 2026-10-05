"""A fake Anthropic API for tests: the real SDK talks to an in-process httpx2 MockTransport, so no
request leaves the process. Responses are synthetic (example.test URLs, ACME), shaped per the
Messages / Message Batches / web-search tool docs (checked 2026-10-05)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx2

from aether.config import LlmConfig, Settings, load_llm_config
from aether.llm.client import LlmClient
from tests.conftest import CONFIG_DIR, make_settings

TEST_API_KEY = "test-key-not-real-0001"  # synthetic; never a real key format
MODEL = "claude-opus-5-5"


def usage(
    input_tokens: int = 1000,
    output_tokens: int = 200,
    searches: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": 0},
    }


def search_result(url: str, title: str, page_age: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "type": "web_search_result",
        "url": url,
        "title": title,
        "encrypted_content": "ZW5jcnlwdGVk",
    }
    if page_age is not None:
        r["page_age"] = page_age
    return r


def message(
    content: Sequence[dict[str, Any]],
    *,
    model: str = MODEL,
    use: dict[str, Any] | None = None,
    msg_id: str = "msg_test_0001",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": list(content),
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": use or usage(),
    }


def research_message(
    results: Sequence[dict[str, Any]],
    citations: Sequence[tuple[str, str]] = (),
    text: str = "Found these pages.",
    searches: int = 1,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_test01",
            "name": "web_search",
            "input": {"query": "ACME announces"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_test01",
            "content": list(results),
        },
        {"type": "text", "text": text},
    ]
    if citations:
        content.append(
            {
                "type": "text",
                "text": "Details.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": u,
                        "title": "t",
                        "encrypted_index": "aW5kZXg=",
                        "cited_text": c,
                    }
                    for u, c in citations
                ],
            }
        )
    return message(content, use=usage(searches=searches))


@dataclass
class FakeApi:
    """Routes requests to canned responses and records every request."""

    messages: list[dict[str, Any]] = field(default_factory=list)  # queue for POST /v1/messages
    batch_status: str = "in_progress"
    batch_results: list[dict[str, Any]] = field(default_factory=list)
    status_code: int = 200
    requests: list[httpx2.Request] = field(default_factory=list)
    on_request: Callable[[httpx2.Request], None] | None = None

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        path = request.url.path
        if self.status_code != 200:
            return httpx2.Response(
                self.status_code,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        if request.method == "POST" and path == "/v1/messages":
            return httpx2.Response(200, json=self.messages.pop(0))
        if request.method == "POST" and path == "/v1/messages/batches":
            return httpx2.Response(200, json=self._batch("in_progress"))
        if request.method == "GET" and path.endswith("/results"):
            body = "\n".join(json.dumps(r) for r in self.batch_results) + "\n"
            return httpx2.Response(
                200, content=body.encode(), headers={"content-type": "application/x-jsonl"}
            )
        if request.method == "GET" and path.startswith("/v1/messages/batches/"):
            return httpx2.Response(200, json=self._batch(self.batch_status))
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})

    def _batch(self, status: str) -> dict[str, Any]:
        ended = status == "ended"
        return {
            "id": "msgbatch_test_0001",
            "type": "message_batch",
            "processing_status": status,
            "request_counts": {
                "processing": 0 if ended else 1,
                "succeeded": 0,
                "errored": 0,
                "canceled": 0,
                "expired": 0,
            },
            "created_at": "2026-10-05T00:00:00Z",
            "expires_at": "2026-10-06T00:00:00Z",
            "ended_at": "2026-10-05T01:00:00Z" if ended else None,
            "cancel_initiated_at": None,
            "archived_at": None,
            "results_url": (
                "https://api.anthropic.com/v1/messages/batches/msgbatch_test_0001/results"
                if ended
                else None
            ),
        }


def llm_settings(db_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"anthropic_api_key": TEST_API_KEY, **overrides}
    return make_settings(db_path, **values)


def llm_config() -> LlmConfig:
    return load_llm_config(CONFIG_DIR)


def make_client(engine: Any, settings: Settings, api: FakeApi, **kw: Any) -> LlmClient:
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api.handler))
    return LlmClient(engine, settings, llm_config(), http_client=http, **kw)
