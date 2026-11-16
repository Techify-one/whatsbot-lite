"""One-shot release gate. Run only by validated v0.3.1 main GitHub Actions job."""
from typing import Any
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

REPO = 'Techify-one/whatsbot-lite'
TAG = 'v0.3.1'

def main() -> None:
    sha = os.environ['TARGET_SHA']
    if not re.fullmatch(r'[0-9a-f]{40}',sha): raise SystemExit('Invalid target SHA')
    def api(path: Any,data: Any=None,optional: Any=False) -> Any:
        request=urllib.request.Request('https://api.github.com/repos/'+REPO+'/'+path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28','User-Agent':'WhatsBot-Lite-0.3.1-release'})
        try:
            with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            if optional and error.code==404:return None
            raise
    version=json.loads(Path('WHATSBOT_VERSION').read_text())
    if version['version']!='0.3.1' or version['changelog'][0]['version']!='0.3.1':raise SystemExit('Wrong source version')
    if api('git/ref/heads/main')['object']['sha']!=sha:raise SystemExit('Main advanced; no stale release')
    existing=api('releases/tags/'+TAG,optional=True)
    tag=api('git/ref/tags/'+TAG,optional=True)
    if existing:
        if not tag or tag['object']['type']!='commit' or tag['object']['sha']!=sha or existing['draft'] or existing['prerelease']:
            raise SystemExit('Existing tag/release differs; never overwrite')
    else:
        if tag:raise SystemExit('Existing tag without matching release; never overwrite')
        body=Path('docs/RELEASE_0.3.1.md').read_text()+'\n\nCommit validado na main: `'+sha+'`.\n'
        existing=api('releases',{'tag_name':TAG,'target_commitish':sha,'name':TAG+' · Classificadores Techify',
                               'body':body,'draft':False,'prerelease':False,'make_latest':'true'})
    if api('git/ref/tags/'+TAG)['object']['sha']!=sha:raise SystemExit('Published tag mismatch')
    print(existing['html_url'])

if __name__=='__main__':main()
