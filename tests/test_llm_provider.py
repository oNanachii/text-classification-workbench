"""Offline provider-contract checks; every message and credential is synthetic."""

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from textlab import llm_provider as provider

TEXT = "Please bring the blue notebook to our meeting tomorrow."
KEY = "synthetic-deepseek-key-never-live"


def good() -> dict:
    """Construct an authored provider response without using a real SMS dataset."""
    return {
        "id": "synthetic-completion-001",
        "model": provider.MODEL,
        "system_fingerprint": "synthetic-fingerprint-001",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"label":"ham"}'},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 6,
            "total_tokens": 106,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class PayloadTests(unittest.TestCase):
    def test_fixed_payload_contains_only_sms_in_the_user_message(self) -> None:
        payload = provider.build_payload(TEXT)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 32)
        self.assertIs(payload["stream"], False)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": provider.SYSTEM_PROMPT},
                {"role": "user", "content": TEXT},
            ],
        )
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertIn("untrusted", provider.SYSTEM_PROMPT)
        self.assertIn("JSON", provider.SYSTEM_PROMPT)
        self.assertNotIn("tools", payload)
        payload["messages"][0]["content"] = "changed only in the returned payload"
        self.assertEqual(
            provider.build_payload(TEXT)["messages"][0]["content"], provider.SYSTEM_PROMPT
        )

    def test_invalid_text_rejected_without_reflecting_it(self) -> None:
        for text in (None, True, "", " \n\t", "x" * (provider.MAX_TEXT_CHARS + 1)):
            with self.subTest(text_type=type(text).__name__):
                with self.assertRaises(ValueError) as caught:
                    provider.build_payload(text)
                self.assertNotIn(KEY, str(caught.exception))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def request_with(self, handler) -> dict:
        """Inject an offline transport so these checks cannot consume provider credit."""
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=True
        ) as client:
            return await provider.classify(client, KEY, TEXT)

    async def test_official_request_and_vetted_numeric_metadata(self) -> None:
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), provider.ENDPOINT)
            self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertEqual(json.loads(request.content), provider.build_payload(TEXT))
            self.assertEqual(request.extensions["timeout"]["read"], 15)
            body = good()
            body["raw_echo"] = KEY + TEXT
            body["usage"]["raw_echo"] = KEY + TEXT
            return httpx.Response(200, json=body, headers={"x-request-id": "synthetic-request-001"})

        result = await self.request_with(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["label"], "ham")
        self.assertFalse(result["failure"])
        self.assertFalse(result["http_failed"])
        self.assertFalse(result["outcome_unknown"])
        self.assertEqual(result["model"], provider.MODEL)
        self.assertEqual(result["provider_id"], "synthetic-completion-001")
        self.assertEqual(result["request_id"], "synthetic-request-001")
        self.assertEqual(result["system_fingerprint"], "synthetic-fingerprint-001")
        self.assertEqual(set(result["usage"]), set(provider.USAGE_FIELDS))
        self.assertEqual(result["usage"]["prompt_cache_miss_tokens"], 20)
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(TEXT, json.dumps(result))

    async def test_invalid_labels_keep_confirmed_usage_and_never_retry(self) -> None:
        contents = [
            " ",
            "ham",
            '{"label":"HAM"}',
            '{"label":"other"}',
            '{"label":"ham","explanation":"' + KEY + '"}',
            '{"label":"ham","label":"spam"}',
            '["ham"]',
            '{"label":null}',
        ]
        for content in contents:
            with self.subTest(content_type=content[:18]):
                calls = []

                def handler(request):
                    calls.append(request)
                    body = good()
                    body["choices"][0]["message"]["content"] = content
                    return httpx.Response(200, json=body)

                result = await self.request_with(handler)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["label"])
                self.assertTrue(result["failure"])
                self.assertEqual(result["usage"]["total_tokens"], 106)
                self.assertNotIn(KEY, json.dumps(result))

    async def test_incomplete_generation_preserves_actual_finish_and_usage(self) -> None:
        for finish in ("length", "content_filter", "tool_calls", "aborted"):
            body = good()
            body["choices"][0]["finish_reason"] = finish
            result = await self.request_with(lambda _: httpx.Response(200, json=body))
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["finish_reason"], finish)
            self.assertEqual(result["error_code"], "provider_generation_incomplete")
            self.assertEqual(result["usage"]["total_tokens"], 106)

    async def test_usage_requires_integer_nonnegative_consistent_counts(self) -> None:
        mutations = [
            lambda u: u.update(prompt_tokens=True),
            lambda u: u.update(completion_tokens=-1),
            lambda u: u.update(total_tokens=105),
            lambda u: u.update(prompt_cache_miss_tokens=19),
            lambda u: u.update(prompt_cache_hit_tokens=101),
            lambda u: u.pop("prompt_cache_miss_tokens"),
            lambda u: u.update(total_tokens="106"),
            lambda u: u.update(prompt_tokens=provider.MAX_USAGE_TOKENS + 1),
            lambda u: u.update(prompt_tokens_details={"cached_tokens": 79}),
            lambda u: u.update(prompt_tokens_details={"cached_tokens": True}),
        ]
        for mutate in mutations:
            body = good()
            mutate(body["usage"])
            result = await self.request_with(lambda _: httpx.Response(200, json=body))
            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["usage"])
            self.assertIsNone(result["label"])

    async def test_http_errors_and_redirects_are_unknown_and_never_followed(self) -> None:
        for status, error in [
            (401, "provider_authentication_failed"),
            (403, "provider_authentication_failed"),
            (429, "provider_rate_limited"),
            (503, "provider_unavailable"),
            (302, "provider_request_rejected"),
        ]:
            calls = []

            def handler(request):
                calls.append(request)
                return httpx.Response(
                    status,
                    json={"error": {"message": KEY + TEXT}},
                    headers={"location": "https://example.invalid/leak"},
                )

            result = await self.request_with(handler)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["http_failed"])
            self.assertTrue(result["failure"])
            self.assertEqual(result["http_status"], status)
            self.assertEqual(result["error_code"], error)
            self.assertNotIn(KEY, json.dumps(result))
            self.assertNotIn(TEXT, json.dumps(result))

    async def test_http_failure_keeps_valid_usage_if_the_provider_reports_it(self) -> None:
        body = good()
        result = await self.request_with(lambda _: httpx.Response(500, json=body))
        self.assertTrue(result["http_failed"])
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["label"])
        self.assertEqual(result["usage"]["total_tokens"], 106)

    async def test_transport_exceptions_are_sanitized_unknown_results(self) -> None:
        for exception, expected in [
            (httpx.ReadTimeout(KEY + TEXT), "provider_timeout"),
            (httpx.ConnectError(KEY + TEXT), "provider_connection_failed"),
        ]:
            calls = []

            def handler(request):
                calls.append(request)
                raise exception

            result = await self.request_with(handler)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["error_code"], expected)
            self.assertIsNone(result["usage"])
            self.assertNotIn(KEY, json.dumps(result))

    async def test_whole_call_timeout_covers_the_response_body(self) -> None:
        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b" "
                await asyncio.sleep(1)
                yield json.dumps(good()).encode()

        def handler(_):
            return httpx.Response(
                200, headers={"content-type": "application/json"}, stream=SlowBody()
            )

        with patch.object(provider, "DEFAULT_TIMEOUT_SECONDS", 0.01):
            result = await self.request_with(handler)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "provider_timeout")
        self.assertGreaterEqual(result["elapsed_ms"], 10)
        self.assertLess(result["elapsed_ms"], 500)

    async def test_transport_completing_after_cancellation_stays_unknown(self) -> None:
        async def handler(_):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            return httpx.Response(200, json=good())

        with patch.object(provider, "DEFAULT_TIMEOUT_SECONDS", 0.01):
            result = await self.request_with(handler)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "provider_timeout")
        self.assertIsNone(result["label"])
        self.assertEqual(result["usage"]["total_tokens"], 106)

    async def test_oversized_body_is_bounded_and_closed(self) -> None:
        body = ChunkedBody([b"x" * provider.MAX_RESPONSE_BYTES, b"x"])
        result = await self.request_with(
            lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=body)
        )
        self.assertTrue(body.closed)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "provider_response_too_large")
        self.assertIsNone(result["usage"])

    async def test_non_json_compression_and_malformed_json_are_unknown(self) -> None:
        cases = [
            httpx.Response(200, text=KEY + TEXT),
            httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
            httpx.Response(
                200,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
                stream=ChunkedBody([b"not-a-gzip-stream"]),
            ),
        ]
        for response in cases:
            result = await self.request_with(lambda _: response)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn(KEY, json.dumps(result))
            self.assertNotIn(TEXT, json.dumps(result))

    async def test_provider_ids_and_model_cannot_reflect_input_or_credentials(self) -> None:
        for value in (KEY, TEXT, "x" * 257, "bad\nmetadata"):
            body = good()
            body["id"] = value
            body["model"] = value
            body["system_fingerprint"] = value
            result = await self.request_with(
                lambda _: httpx.Response(200, json=body, headers={"x-request-id": KEY})
            )
            self.assertIsNone(result["provider_id"])
            self.assertIsNone(result["request_id"])
            self.assertIsNone(result["model"])
            self.assertIsNone(result["system_fingerprint"])
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn(KEY, json.dumps(result))
            self.assertNotIn(TEXT, json.dumps(result))

    async def test_optional_system_fingerprint_is_not_required_for_a_label(self) -> None:
        body = good()
        body.pop("system_fingerprint")
        result = await self.request_with(lambda _: httpx.Response(200, json=body))
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["system_fingerprint"])

    async def test_model_mismatch_is_retained_but_not_scored_as_requested_model(self) -> None:
        body = good()
        body["model"] = "deepseek-unexpected-model"
        result = await self.request_with(lambda _: httpx.Response(200, json=body))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["model"], "deepseek-unexpected-model")
        self.assertEqual(result["error_code"], "provider_model_mismatch")
        self.assertEqual(result["usage"]["total_tokens"], 106)

    async def test_extra_choices_or_tool_output_never_become_a_label(self) -> None:
        bodies = []
        body = good()
        body["choices"].append(copy.deepcopy(body["choices"][0]))
        bodies.append(body)
        body = good()
        body["choices"][0]["message"]["tool_calls"] = [{"raw": KEY + TEXT}]
        bodies.append(body)
        for body in bodies:
            result = await self.request_with(lambda _: httpx.Response(200, json=body))
            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["label"])
            self.assertNotIn(KEY, json.dumps(result))

    async def test_invalid_credential_never_dispatches(self) -> None:
        calls = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(request)), trust_env=False
        ) as client:
            for key in ("", "line\nbreak", "contains space", "非ASCII", True):
                with self.assertRaises(ValueError):
                    await provider.classify(client, key, TEXT)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
