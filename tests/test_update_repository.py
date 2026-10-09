"""Dependency-free tests of the updater's release lookup after repository rename."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
source = ast.parse((ROOT / 'server/routes/update.py').read_text())
# Exercise actual pure updater code without importing the database/server package.
source.body = [node for node in source.body if isinstance(node, (ast.Import, ast.Assign, ast.FunctionDef))]
namespace = {'Path': Path}
exec(compile(source, 'server/routes/update.py', 'exec'), namespace)

class Response:
    def __init__(self, data=b'', url=''):
        self.data, self.url = data, url
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self): return self.data
    def geturl(self): return self.url

class UpdateRepositoryTest(unittest.TestCase):
    def test_all_release_urls_use_current_repository(self):
        self.assertEqual(namespace['GITHUB_REPO'], 'Techify-one/whatsbot-lite')
        for key in ['GITHUB_RELEASES_API', 'GITHUB_LATEST_RELEASE_URL',
                    'GITHUB_RAW_VERSION_URL_TEMPLATE', 'GITHUB_TAG_ZIP_URL_TEMPLATE']:
            self.assertIn('/Techify-one/whatsbot-lite/', namespace[key])
        self.assertEqual(namespace['GITHUB_TAG_ZIP_URL_TEMPLATE'].format(tag='v0.2.10'),
                         'https://github.com/Techify-one/whatsbot-lite/archive/refs/tags/v0.2.10.zip')

    def test_public_release_fallback_uses_current_tag(self):
        urls = []
        def urlopen(request, **_):
            urls.append(request.full_url)
            if request.full_url.endswith('/releases/latest'):
                return Response(url='https://github.com/Techify-one/whatsbot-lite/releases/tag/v0.2.10')
            return Response(json.dumps({'version': '0.2.10', 'changelog': [
                {'version': '0.2.10', 'description': '- PWA'}]}).encode())
        with patch('urllib.request.urlopen', side_effect=urlopen):
            release = namespace['_fetch_release_without_api']()
        self.assertEqual(release['version'], '0.2.10')
        self.assertEqual(release['description'], '- PWA')
        self.assertEqual(urls, [namespace['GITHUB_LATEST_RELEASE_URL'],
            'https://raw.githubusercontent.com/Techify-one/whatsbot-lite/v0.2.10/WHATSBOT_VERSION'])

if __name__ == '__main__': unittest.main()
