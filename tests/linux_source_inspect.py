"""Smoke and rejection checks: python tests/linux_source_inspect.py BLENDER CORE WORKDIR.

WORKDIR must be a fresh private directory on a qualified filesystem. No renderer
is invoked. The CLI bundle is copied there (implementation files are guarded).
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

blender, core, root = map(lambda value: Path(value).resolve(), sys.argv[1:])
root.mkdir(mode=0o700, exist_ok=False)
bundle = root / 'toon_workflow'
shutil.copytree(Path(__file__).resolve().parents[1] / 'toon_workflow', bundle)
env = dict(os.environ, TOON_WORKFLOW_CLI_CORE_ROOT=str(core), PYTHONDONTWRITEBYTECODE='1',
           BLENDER_USER_RESOURCES=str(root/'profile'))
source = root/'source.blend'
subprocess.run([str(blender), '-b', '-t', '1', '--factory-startup', '--disable-autoexec',
                '--offline-mode', '--python-exit-code', '5', '--python-expr',
                f'import bpy; bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})'], env=env, check=True)
sys.path.insert(0, str(root))
from toon_workflow.toon_workflow_addon.workflow_contract import request, fingerprint
params = request(str(source),'Scene','ViewLayer',[1],resources=[])['params']
def inspect(params):
    path = root/'params.json'
    path.write_text(json.dumps(params))
    proc = subprocess.run([sys.executable, str(bundle/'toon_workflow_cli_v1/cli.py'),
        '--blender',str(blender),'--jobs-dir',str(root/'jobs'),'toon-workflow','inspect','--params',str(path)],
        env=env, stdout=subprocess.PIPE, text=True)
    result=json.loads(proc.stdout)
    assert (proc.returncode==0) == result['ok'], result
    return result
result=inspect(params)
assert result['ok'], result
data=result['data']
assert data['source_saved'] is False and data['source_unchanged'] is True
assert data['source_sha256']==params['expected_sha256']==sha(source)
assert {Path(row['file']).name for row in data['outputs']}=={'toon-workflow-report.json','toon-author.json'}
for row in data['outputs']:
    assert sha(Path(row['file']))==row['expected_sha256']
author=json.loads(Path(data['author']['file']).read_text())
assert data['author_sha256']==author['author_sha256']==fingerprint(author['author'])
bad=inspect(dict(params,expected_sha256='0'*64))
assert bad['error']['code']=='CONFLICT',bad
print('PASS: source, author and output fingerprints; incorrect source hash rejected')
