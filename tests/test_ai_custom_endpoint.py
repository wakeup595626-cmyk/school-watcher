"""Custom OpenAI-compatible endpoints: validation, isolation and dispatch.

No live provider calls: a local in-process HTTP fixture stands in for the
administrator's own service.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from backend.ai.configuration import AIConfigError, credential_for, save_profile, _metadata
from backend.ai.models import AIExecution, AIProfile
from backend.ai.providers import ProviderResult, complete, normalize_custom_endpoint
from backend.ai.runtime import test_connection
from backend.database.db import db
import test_ai_runtime


class CustomEndpointValidationTests(unittest.TestCase):
    def test_base_url_and_full_chat_path_are_normalized(self):
        self.assertEqual(normalize_custom_endpoint('https://relay.example.com/v1'),
                         'https://relay.example.com/v1/chat/completions')
        self.assertEqual(normalize_custom_endpoint(' https://relay.example.com/v1/ '),
                         'https://relay.example.com/v1/chat/completions')
        full = 'https://relay.example.com/api/chat/completions'
        self.assertEqual(normalize_custom_endpoint(full), full)
        self.assertEqual(normalize_custom_endpoint('http://127.0.0.1:8317/v1'),
                         'http://127.0.0.1:8317/v1/chat/completions')

    def test_invalid_endpoints_are_rejected(self):
        for value in ('', '   ', 'ftp://relay.example.com/v1', 'relay.example.com/v1',
                      'https://user:pass@relay.example.com/v1',
                      'https://relay.example.com/v1?token=secret',
                      'https://relay.example.com/v1#frag',
                      'https://relay.example.com/v1 with space',
                      'https://relay.example.com:abc/v1',
                      'https://relay.example.com:0/v1',
                      'https://relay.example.com:65536/v1',
                      'https://relay.example.com/' + 'a' * 2100):
            with self.subTest(value=value[:60]):
                with self.assertRaises(ValueError):
                    normalize_custom_endpoint(value)

    def test_complete_rejects_custom_profile_without_stored_url(self):
        with self.assertRaises(ValueError):
            complete({'provider': 'custom', 'region': 'default', 'model': 'x', 'base_url': ''}, 'k', [])


class CustomEndpointDispatchTests(unittest.TestCase):
    """Point a custom profile at a local HTTP fixture and verify the wire format."""

    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server

    def test_custom_profile_sends_openai_shape_to_stored_url(self):
        captured = {}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured['path'] = self.path
                captured['authorization'] = self.headers.get('Authorization')
                length = int(self.headers.get('Content-Length') or 0)
                captured['body'] = json.loads(self.rfile.read(length))
                payload = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 5, 'completion_tokens': 2}, 'id': 'fixture-1'}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = self.serve(Handler)
        base = 'http://127.0.0.1:' + str(server.server_port) + '/v1'
        profile = {'provider': 'custom', 'region': 'default', 'model': 'fixture-model', 'base_url': base}
        result = complete(profile, 'fixture-custom-key', [{'role': 'user', 'content': 'JSON'}])
        self.assertEqual(result.content, '{"ok":true}')
        self.assertEqual(result.usage['total_tokens'], 7)
        self.assertEqual(captured['path'], '/v1/chat/completions')
        self.assertEqual(captured['authorization'], 'Bearer fixture-custom-key')
        body = captured['body']
        self.assertEqual(body['model'], 'fixture-model')
        self.assertIn('messages', body)
        self.assertNotIn('response_format', body)
        self.assertNotIn('thinking', body)
        self.assertFalse(body['stream'])


class CustomProfileConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ai_runtime.AIRuntimeTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def save_custom(self, **overrides):
        payload = {'provider': 'custom', 'model': 'fixture-model',
                   'base_url': 'https://relay.fixture.example/v1', 'api_key': 'fixture-custom-key'}
        payload.update(overrides)
        return save_profile(payload)

    def test_save_custom_profile_validates_and_stores_normalized_url(self):
        saved = self.save_custom(name='\u6211\u7684\u4e2d\u8f6c')
        self.assertEqual(saved['provider'], 'custom')
        self.assertEqual(saved['region'], 'default')
        self.assertEqual(saved['base_url'], 'https://relay.fixture.example/v1/chat/completions')
        self.assertTrue(saved['has_key'])
        self.assertFalse(saved['enabled'])
        self.assertFalse(saved['tested'])
        row = db.session.get(AIProfile, saved['id'])
        self.assertEqual(row.base_url, 'https://relay.fixture.example/v1/chat/completions')

    def test_custom_profile_requires_base_url_and_rejects_bad_urls(self):
        with self.assertRaises(AIConfigError):
            self.save_custom(base_url='')
        with self.assertRaises(AIConfigError):
            self.save_custom(base_url='ftp://relay.fixture.example/v1')
        with self.assertRaises(AIConfigError):
            self.save_custom(base_url='https://user:pass@relay.fixture.example/v1')

    def test_fixed_provider_cannot_smuggle_a_base_url(self):
        with self.assertRaises(AIConfigError):
            save_profile({'provider': 'deepseek', 'model': 'fixture',
                          'base_url': 'https://evil.example/v1', 'api_key': 'fixture-key'},
                         self.fixture.binding['id'])
        row = db.session.get(AIProfile, self.fixture.binding['id'])
        self.assertEqual(row.base_url or '', '')

    def test_url_change_bumps_version_and_requires_retest(self):
        saved = self.save_custom()
        profile = db.session.get(AIProfile, saved['id'])
        profile.enabled, profile.tested_version = True, profile.version
        db.session.commit()
        before = profile.version
        updated = save_profile({'base_url': 'https://other.fixture.example/v2'}, saved['id'])
        self.assertEqual(updated['version'], before + 1)
        self.assertEqual(updated['base_url'], 'https://other.fixture.example/v2/chat/completions')
        self.assertFalse(updated['enabled'])
        self.assertFalse(updated['tested'])
        with self.assertRaises(AIConfigError):
            credential_for(_metadata(db.session.get(AIProfile, saved['id'])), allow_untested=False)

    def test_binding_url_mismatch_cannot_redirect_the_key(self):
        saved = self.save_custom()
        profile = db.session.get(AIProfile, saved['id'])
        profile.enabled, profile.tested_version = True, profile.version
        db.session.commit()
        binding = _metadata(profile)
        tampered = dict(binding, base_url='https://evil.fixture.example/v1/chat/completions')
        with self.assertRaises(AIConfigError) as caught:
            credential_for(tampered)
        self.assertEqual(caught.exception.code, 'configuration_changed')

    def test_credential_for_returns_key_and_binding_carries_base_url(self):
        saved = self.save_custom()
        profile = db.session.get(AIProfile, saved['id'])
        profile.enabled, profile.tested_version = True, profile.version
        db.session.commit()
        binding = _metadata(profile)
        self.assertEqual(credential_for(binding), 'fixture-custom-key')
        self.assertEqual(binding['base_url'], 'https://relay.fixture.example/v1/chat/completions')

    def test_public_settings_lists_custom_provider_without_key_page(self):
        from backend.ai.configuration import public_settings
        providers = {p['id']: p for p in public_settings()['providers']}
        self.assertIn('custom', providers)
        self.assertEqual(providers['custom']['key_url'], '')
        self.assertEqual(providers['custom']['regions'], ['default'])
        self.assertTrue(providers['custom']['custom'])

    def test_connection_test_dispatches_to_custom_url(self):
        captured = {}
        from backend.ai import providers as provider_module
        def spy(profile, key, messages, **kwargs):
            captured['url'] = provider_module.request_url_for(profile)
            captured['key'] = key
            return ProviderResult('{"ok":true}', 'stop',
                {'known': True, 'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12}, 'fixture')
        saved = self.save_custom()
        with patch('backend.ai.providers.complete', side_effect=spy):
            result = test_connection(saved['id'])
        self.assertTrue(result['tested'])
        self.assertEqual(captured['url'], 'https://relay.fixture.example/v1/chat/completions')
        self.assertEqual(captured['key'], 'fixture-custom-key')
        row = db.session.get(AIProfile, saved['id'])
        self.assertTrue(row.enabled)
        self.assertEqual(row.tested_version, row.version)

    def test_connection_test_succeeds_when_custom_gateway_omits_usage(self):
        def spy(profile, key, messages, **kwargs):
            return ProviderResult('{"ok":true}', 'stop', {'known': False}, 'fixture')
        saved = self.save_custom()
        with patch('backend.ai.providers.complete', side_effect=spy):
            result = test_connection(saved['id'])
        self.assertTrue(result['tested'])
        self.assertEqual(result['test_code'], 'ok')
        self.assertFalse(result['usage_reported'])
        row = db.session.get(AIProfile, saved['id'])
        self.assertTrue(row.enabled)
        execution = db.session.get(AIExecution, result['provenance']['execution_id'])
        self.assertFalse(execution.usage['known'])
        self.assertEqual(execution.reserved_tokens, 0)


class CustomEndpointMigrationTests(unittest.TestCase):
    def test_upgrade_adds_base_url_and_existing_rows_keep_empty_string(self):
        from datetime import datetime
        import os
        import tempfile
        from pathlib import Path
        import sqlalchemy as sa
        from flask_migrate import upgrade
        from backend import create_app
        from backend.core.config import ROOT_DIR
        tmp = tempfile.TemporaryDirectory(prefix='watcher-custom-migrate-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with patch.dict(os.environ, {'SECRET_KEY': 'isolated-test'}):
            app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-test',
                'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'main.db'),
                'SOURCE_CATALOG_PATH': str(root / 'catalog.sqlite3'),
                'BACKUP_DIR': str(root / 'backups'), 'BACKUP_COPY_DIR': ''})
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(lambda: (db.session.remove(), db.engine.dispose(), ctx.pop()))
        with app.app_context():
            upgrade(directory=str(ROOT_DIR / 'migrations'), revision='05b7c8123a90')
            profiles = sa.Table('ai_profiles', sa.MetaData(), autoload_with=db.engine)
            with db.engine.begin() as connection:
                connection.execute(profiles.insert().values(
                    name='existing service', provider='deepseek', model='fixture', region='default',
                    encrypted_key='enc:fixture', version=1, tested_version=1, enabled=True,
                    max_output_tokens=4096, last_test_code='ok',
                    created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
            upgrade(directory=str(ROOT_DIR / 'migrations'))
            row = AIProfile.query.one()
            self.assertEqual(row.base_url, '')
            self.assertTrue(row.enabled)
            meta = _metadata(row)
            self.assertEqual(meta['base_url'], '')


if __name__ == '__main__':
    unittest.main()

