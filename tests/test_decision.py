import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app_v2 import create_app
from decision import (DecisionRequest, DecisionService, DecisionSettings, ENDPOINT, MODEL,
                      MAX_RESPONSE_BYTES, parse_response)

KEY = 'authored-dummy-key-not-a-secret'
TEXT = 'Please bring the blue notebook tomorrow.'
LOADED = {'kind': 'lr', 'run_id': 'synthetic-model', 'threshold': .5, 'snapshot_id': 'synthetic-snapshot', 'dataset_id': 'synthetic-data'}


def good():
    return {'model': MODEL, 'answers': {'sms_class': {'type': 'choice', 'choice': 'ham', 'probabilities': {'ham': .9, 'spam': .1}, 'confidence': .8}}, 'usage': {'input_tokens': 60, 'output_tokens': 10}}


class FakeModel:
    def predict(self, text, threshold):
        return {'label': 'spam', 'spam_score': .8, 'threshold': threshold}


class DecisionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def request_with(self, handler, deadline=15):
        service = DecisionService(DecisionSettings(True, False), httpx.MockTransport(handler), deadline)
        return await service.jev(TEXT, KEY)

    async def test_official_fixed_contract_and_only_whitelisted_response(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), ENDPOINT)
            self.assertEqual(request.headers['authorization'], 'Bearer ' + KEY)
            body = json.loads(request.content)
            self.assertEqual(body['state'], TEXT)
            self.assertEqual(body['model'], MODEL)
            self.assertEqual(set(body['questions']['sms_class']['criteria']), {'ham', 'spam'})
            self.assertNotIn(KEY, request.content.decode())
            response = good();response['injected'] = KEY;response['usage']['echo'] = KEY
            return httpx.Response(200, json=response)
        result = await self.request_with(handler)
        self.assertEqual(result['status'], 'ok');self.assertEqual(result['confidence_kind'], 'choice_distribution_concentration')
        self.assertNotIn(KEY, json.dumps(result));self.assertNotIn(TEXT, json.dumps(result));self.assertEqual(len(calls), 1)
        self.assertEqual(result['usage'], {'input_tokens': 60, 'output_tokens': 10})

    async def test_provider_errors_are_sanitized_and_never_retried(self):
        for status, expected in [(401, 'provider_authentication_failed'), (403, 'provider_authentication_failed'), (429, 'provider_rate_limited'), (500, 'provider_unavailable'), (302, 'provider_request_rejected')]:
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, json={'error': KEY + TEXT}, headers={'location': 'https://example.invalid/'})
                result = await self.request_with(handler)
                self.assertEqual(result['error_code'], expected);self.assertEqual(len(calls), 1)
                self.assertNotIn(KEY, json.dumps(result));self.assertNotIn(TEXT, json.dumps(result))

    async def test_non_json_and_oversized_responses(self):
        cases = [httpx.Response(200, text=KEY), httpx.Response(200, content=b'no-json', headers={'content-type': 'application/json'}), httpx.Response(200, content=b' ' * (MAX_RESPONSE_BYTES + 1), headers={'content-type': 'application/json'})]
        for response in cases:
            result = await self.request_with(lambda _: response)
            self.assertNotEqual(result['status'], 'ok');self.assertTrue(result['outcome_unknown']);self.assertNotIn(KEY, json.dumps(result))

    async def test_timeout_connection_and_late_response_are_unknown(self):
        for exception in (httpx.ReadTimeout(KEY), httpx.ConnectError(KEY)):
            def handler(_):raise exception
            result = await self.request_with(handler)
            self.assertEqual(result['status'], 'unknown');self.assertNotIn(KEY, json.dumps(result))
        async def late(_):
            try:await asyncio.sleep(.1)
            except asyncio.CancelledError:pass  # simulate a transport completing after cancellation
            return httpx.Response(200, json=good())
        result = await self.request_with(late, .01)
        self.assertEqual(result['error_code'], 'provider_timeout');self.assertTrue(result['outcome_unknown'])

    async def test_compare_keeps_local_result_on_provider_failure(self):
        service = DecisionService(DecisionSettings(True), httpx.MockTransport(lambda _: httpx.Response(429, json={'error': KEY})))
        body = DecisionRequest(text=TEXT, engine='compare', external_consent=True)
        result = await service.decide(body, FakeModel(), LOADED, KEY)
        self.assertEqual(result['local']['status'], 'ok');self.assertEqual(result['jev']['status'], 'error');self.assertIsNone(result['disagreement'])
        self.assertTrue(result['human_review_required']);self.assertNotIn(TEXT, json.dumps(result))
        service.transport = httpx.MockTransport(lambda _: httpx.Response(200, json=good()))
        result = await service.decide(body, FakeModel(), LOADED, KEY)
        self.assertTrue(result['disagreement']);self.assertIsNone(result['local']['confidence'])


class DecisionSchemaTests(unittest.TestCase):
    def test_invalid_labels_probabilities_versions_usage_and_confidence_rejected(self):
        mutations = [
            lambda d: d.update(model='jev-latest'),
            lambda d: d['answers']['sms_class'].update(choice='new_label'),
            lambda d: d['answers']['sms_class'].update(choice='spam'),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': 1.2, 'spam': -.2}),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': .9, 'spam': .3}),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': True, 'spam': False}),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': float('nan'), 'spam': .1}),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': float('inf'), 'spam': .1}),
            lambda d: d['answers']['sms_class'].update(probabilities={'ham': .9, 'spam': .1, 'extra': 0}),
            lambda d: d['answers']['sms_class'].update(confidence=True),
            lambda d: d['answers']['sms_class'].update(confidence=.2),
            lambda d: d.update(usage={'input_tokens': -1, 'output_tokens': 10}),
            lambda d: d.update(usage={'input_tokens': 2, 'output_tokens': KEY}),
        ]
        for mutate in mutations:
            value = good();mutate(value)
            with self.assertRaises(ValueError):parse_response(value)

    def test_ties_and_missing_usage_are_represented_honestly(self):
        value = good();value['answers']['sms_class'].update(probabilities={'ham': .5, 'spam': .5}, confidence=0);value.pop('usage')
        result = parse_response(value);self.assertEqual(result['confidence'], 0);self.assertIsNone(result['usage'])

    def test_rate_limits_and_concurrency_release(self):
        service = DecisionService()
        service.acquire('a');service.acquire('b')
        with self.assertRaises(HTTPException) as caught:service.acquire('c')
        self.assertEqual(caught.exception.status_code, 429)
        service.release();service.release()
        for _ in range(5):service.acquire('a');service.release()
        with self.assertRaises(HTTPException):service.acquire('a')
        for i in range(23):service.acquire(str(i));service.release()
        with self.assertRaises(HTTPException):service.acquire('global-full')


class DecisionAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.calls = []
        def handler(request):
            self.calls.append({'auth': request.headers.get('authorization'), 'body': request.content.decode()})
            return httpx.Response(200, json=good())
        self.service = DecisionService(DecisionSettings(True, True), httpx.MockTransport(handler))
        self.body = {'text': TEXT, 'engine': 'compare', 'credential_source': 'byok', 'external_consent': True}
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': 'synthetic-server-key'});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def client(self, public=True, base_url='https://testserver'):
        app = create_app(self.temp.name, 'synthetic-operator', public, self.service)
        # No project dataset/model is touched. The fixture injects a numeric interface only.
        client = TestClient(app, base_url=base_url, client=('127.0.0.1', 50000))
        return app, client

    def test_public_byok_has_no_fallback_and_keeps_private_writes_blocked(self):
        app, client = self.client()
        with client as c:
            app.state.model = FakeModel();app.state.loaded = LOADED
            response = c.post('/api/decision', json=self.body)
            self.assertEqual(response.status_code, 400);self.assertEqual(self.calls, [])
            response = c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': KEY})
            self.assertEqual(response.status_code, 200);self.assertTrue(response.json()['disagreement'])
            self.assertEqual(self.calls[0]['auth'], 'Bearer ' + KEY);self.assertNotIn(KEY, response.text);self.assertNotIn(TEXT, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            headers = {'Authorization': 'Bearer synthetic-operator'}
            self.assertEqual(c.post('/api/decision', json=self.body | {'credential_source': 'server'}, headers=headers).status_code, 403)
            self.assertEqual(c.post('/api/snapshots', headers=headers).status_code, 403)
            self.assertFalse(c.get('/api/config', headers=headers).json()['decision']['server_key_available'])
            self.assertEqual(len(self.calls), 1)

    def test_server_key_requires_nonpublic_authenticated_explicit_selection(self):
        app, client = self.client(public=False)
        with client as c:
            server = self.body | {'credential_source': 'server'}
            self.assertEqual(c.post('/api/decision', json=server).status_code, 403)
            headers = {'Authorization': 'Bearer synthetic-operator'}
            self.assertTrue(c.get('/api/config', headers=headers).json()['decision']['server_key_available'])
            self.assertEqual(c.post('/api/decision', json=server, headers=headers).status_code, 200)
            self.assertEqual(self.calls[0]['auth'], 'Bearer synthetic-server-key')
            self.service.settings = DecisionSettings(True, False)
            self.assertEqual(c.post('/api/decision', json=server, headers=headers).status_code, 403)

    def test_explicit_consent_tls_and_same_origin_are_required(self):
        app, client = self.client(base_url='http://testserver')
        with client as c:
            response = c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': KEY, 'X-Forwarded-Proto': 'https'})
            self.assertEqual(response.status_code, 400)
        app, client = self.client()
        with client as c:
            self.assertEqual(c.post('/api/decision', json=self.body | {'external_consent': False}, headers={'X-TypeSafe-Key': KEY}).status_code, 400)
            self.assertEqual(c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': KEY, 'Origin': 'https://attacker.invalid'}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_loopback_http_is_only_allowed_in_nonpublic_development(self):
        _, client = self.client(public=False, base_url='http://127.0.0.1')
        with client as c:self.assertEqual(c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': KEY}).status_code, 200)
        self.assertEqual(len(self.calls), 1)

    def test_input_and_credentials_are_not_echoed_in_any_error(self):
        _, client = self.client()
        with client as c:
            for body in [self.body | {'text': ' '}, self.body | {'text': 'x' * 2001}, self.body | {'api_key': KEY}, self.body | {'engine': KEY}]:
                response = c.post('/api/decision', json=body, headers={'X-TypeSafe-Key': KEY})
                self.assertEqual(response.status_code, 422);self.assertNotIn(KEY, response.text);self.assertNotIn(TEXT, response.text)
            response = c.post('/api/decision', content='x' * (16 * 1024 + 1), headers={'Content-Type': 'application/json', 'X-TypeSafe-Key': KEY})
            self.assertEqual(response.status_code, 413);self.assertEqual(response.headers['cache-control'], 'no-store')
            response = c.post('/api/decision', content=json.dumps(self.body), headers={'Content-Type': 'text/plain'})
            self.assertEqual(response.status_code, 415)
            response = c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': 'key with spaces'})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_disabled_service_does_not_call_or_block_local(self):
        self.service.settings = DecisionSettings(False, False)
        app, client = self.client()
        with client as c:
            app.state.model = FakeModel();app.state.loaded = LOADED
            self.assertEqual(c.post('/api/decision', json=self.body, headers={'X-TypeSafe-Key': KEY}).status_code, 503)
            response = c.post('/api/decision', json={'text': TEXT, 'engine': 'local'})
            self.assertEqual(response.status_code, 200);self.assertIsNone(response.json()['jev']);self.assertEqual(response.json()['local']['label'], 'spam')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':unittest.main()
