"""Dependency-free tests of the update mode switch (local vs WhatsBot Cloud)."""
import ast
import io
import json
import os
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
source = ast.parse((ROOT / 'server/routes/update.py').read_text())
# Exercise actual pure updater code without importing the database/server package.
source.body = [node for node in source.body if isinstance(node, (ast.Import, ast.Assign, ast.FunctionDef))]
namespace = {'Path': Path}
exec(compile(source, 'server/routes/update.py', 'exec'), namespace)

UUID = '874b8da7-4a3f-4ec3-ba8b-facca9898c7f'
TASK = 'd3faeaae8d5a4b17b2492502fd2da23d'


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self): return json.dumps(self.data).encode()


def http_error(code, body):
    return urllib.error.HTTPError('u', code, 'err', {}, io.BytesIO(json.dumps(body).encode()))


class UpdateModeTest(unittest.TestCase):
    def test_local_is_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(namespace['_update_mode'](), 'local')
        with patch.dict(os.environ, {'WHATS_UPDATE_MODE': 'qualquer'}):
            self.assertEqual(namespace['_update_mode'](), 'local')
        with patch.dict(os.environ, {'WHATS_UPDATE_MODE': ' Cloud '}):
            self.assertEqual(namespace['_update_mode'](), 'cloud')

    def test_cloud_url_is_configurable_with_production_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(namespace['_cloud_update_url'](), namespace['DEFAULT_CLOUD_UPDATE_URL'])
        with patch.dict(os.environ, {'WHATS_UPDATE_URL': 'http://10.0.0.5:9000/api/updates/'}):
            self.assertEqual(namespace['_cloud_update_url'](), 'http://10.0.0.5:9000/api/updates')


class CloudRequestTest(unittest.TestCase):
    env = {'WHATS_UPDATE_MODE': 'cloud', 'WHATSBOT_INSTALLATION_ID': UUID,
           'WHATS_UPDATE_TOKEN': 'a' * 64, 'WHATS_UPDATE_URL': 'https://upd.example/api/updates'}

    def test_start_sends_id_version_and_token(self):
        seen = {}
        def urlopen(req, **_):
            seen.update(url=req.full_url, method=req.get_method(), body=json.loads(req.data),
                        token=req.get_header('X-installation-token'))
            return Response({'task_id': TASK, 'status': 'running', 'phase': 'queued'}, 202)
        with patch.dict(os.environ, self.env), patch('urllib.request.urlopen', side_effect=urlopen):
            task = namespace['_start_cloud_update'](UUID, 'v0.2.14')
        self.assertEqual(task['task_id'], TASK)
        self.assertEqual(seen, {'url': 'https://upd.example/api/updates', 'method': 'POST',
                                'body': {'installation_id': UUID, 'version': 'v0.2.14'}, 'token': 'a' * 64})

    def test_start_maps_server_errors(self):
        with patch.dict(os.environ, self.env), \
                patch('urllib.request.urlopen', side_effect=http_error(404, {'detail': 'version_not_found'})):
            with self.assertRaisesRegex(RuntimeError, 'não foi encontrada'):
                namespace['_start_cloud_update'](UUID, 'v9.9.9')
        with patch.dict(os.environ, self.env), \
                patch('urllib.request.urlopen',
                      side_effect=http_error(409, {'detail': {'error': 'update_in_progress', 'task_id': TASK}})):
            with self.assertRaisesRegex(RuntimeError, 'em andamento'):
                namespace['_start_cloud_update'](UUID, 'v0.2.14')

    def test_status(self):
        def urlopen(req, **_):
            self.assertEqual(req.full_url, f'https://upd.example/api/updates/{TASK}')
            return Response({'status': 'failed', 'phase': 'health', 'version': 'v0.2.14', 'error': 'unhealthy'})
        with patch.dict(os.environ, self.env), patch('urllib.request.urlopen', side_effect=urlopen):
            st = namespace['_cloud_update_status'](TASK)
        self.assertEqual(st['status'], 'failed')
        self.assertEqual(st['version'], '0.2.14')
        self.assertIn('não iniciou', st['error'])


if __name__ == '__main__': unittest.main()
