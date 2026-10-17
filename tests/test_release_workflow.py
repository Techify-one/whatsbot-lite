"""Offline verification of the one-shot release script; never contacts GitHub."""
import base64
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/release-pwa-0.2.10.yml').read_text())
RUN = WORKFLOW['jobs']['publish']['steps'][0]['run']
CODE = RUN.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
SHA = 'a' * 40
TAG = {'object': {'type': 'commit', 'sha': SHA}}
RELEASE = {'tag_name': 'v0.2.10', 'html_url': 'https://github.com/Techify-one/whatsbot-lite/releases/tag/v0.2.10', 'draft': False, 'prerelease': False}

class ReleaseWorkflowTest(unittest.TestCase):
    def run_script(self, existing=False, tag_exists=False, main_sha=SHA, version='0.2.10'):
        posts = []
        created = False
        def urlopen(request, **_):
            nonlocal created
            path = request.full_url.split('/repos/Techify-one/whatsbot-lite/')[1]
            if request.data is not None:
                data = json.loads(request.data)
                posts.append(data)
                self.assertEqual(path, 'releases')
                self.assertEqual(data['target_commitish'], SHA)
                self.assertEqual(data['tag_name'], 'v0.2.10')
                self.assertFalse(data['draft'])
                self.assertFalse(data['prerelease'])
                created = True
                result = RELEASE
            elif path.startswith('contents/'):
                name = path.removeprefix('contents/').split('?')[0]
                if name == 'WHATSBOT_VERSION':
                    data = json.dumps({'version': version, 'changelog': [{'version': version, 'description': '- PWA'}]})
                else:
                    data = (ROOT / name).read_text()
                result = {'content': base64.b64encode(data.encode()).decode()}
            elif path == 'git/ref/heads/main': result = {'object': {'sha': main_sha}}
            elif path == 'releases/latest': result = RELEASE
            elif path == 'releases/tags/v0.2.10' and existing: result = RELEASE
            elif path == 'git/ref/tags/v0.2.10' and (existing or tag_exists or created): result = TAG
            else: raise urllib.error.HTTPError(request.full_url, 404, 'Not found', {}, None)
            return io.BytesIO(json.dumps(result).encode())
        with patch.dict(os.environ, {'TARGET_SHA': SHA, 'GH_TOKEN': 'test-ephemeral-token'}), patch('urllib.request.urlopen', side_effect=urlopen):
            exec(compile(CODE, '<release-workflow>', 'exec'), {})
        return posts

    def test_publish_exact_version_and_commit(self):
        self.assertEqual(len(self.run_script()), 1)

    def test_rerun_does_not_modify_existing_release(self):
        self.assertEqual(self.run_script(existing=True), [])

    def test_existing_tag_is_never_overwritten(self):
        with self.assertRaisesRegex(SystemExit, 'Tag already exists'): self.run_script(tag_exists=True)

    def test_main_advance_stops_publication(self):
        with self.assertRaisesRegex(SystemExit, 'Main advanced'): self.run_script(main_sha='b' * 40)

    def test_other_version_stops_publication(self):
        with self.assertRaisesRegex(SystemExit, 'exactly 0.2.10'): self.run_script(version='0.2.11')

if __name__ == '__main__': unittest.main()
