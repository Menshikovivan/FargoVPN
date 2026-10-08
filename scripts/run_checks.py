#!/usr/bin/env python3
"""One-command isolated checks. Never load production config or mutate services."""
import argparse
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--without-dom',action='store_true',help='Explicitly skip browser DOM tests; results are partial')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    env=dict(os.environ)
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"]=os.pathsep.join(str(Path(item).resolve()) for item in env["PYTHONPATH"].split(os.pathsep) if item)
    node=shutil.which('node')
    if not node:parser.error('Install Node.js for JavaScript syntax/DOM checks')
    if not args.without_dom and not env.get('FARGOVPN_JSDOM_PATH'):
        local=root/'.qa-js/node_modules/jsdom'
        if local.exists():env['FARGOVPN_JSDOM_PATH']=str(local)
        else:parser.error('Install jsdom: npm install --prefix .qa-js jsdom@26; or set FARGOVPN_JSDOM_PATH')
    if args.without_dom:env.pop('FARGOVPN_JSDOM_PATH',None)
    for path in root.rglob('*.py'):
        if not any(part.startswith('.') or part=='node_modules' for part in path.relative_to(root).parts):ast.parse(path.read_text(),filename=str(path))
    for path in root.rglob('*.sh'):
        if not any(part.startswith('.') or part=='node_modules' for part in path.relative_to(root).parts):subprocess.run(['bash','-n',str(path)],check=True)
    for path in [root/'service-worker.js',* (root/'static').glob('*.js')]:subprocess.run([node,'--check',str(path)],check=True)
    copies=[(root/name).read_text().strip() for name in ['VERSION','app/VERSION','static/VERSION']]
    if len(set(copies))!=1:raise RuntimeError('Version copies differ')
    with tempfile.TemporaryDirectory(prefix='fargovpn-qa-') as temporary:
        result_file=Path(temporary)/'results.xml'
        result=subprocess.call([sys.executable,'-m','pytest','-q',str(root/'tests'),'--basetemp',str(Path(temporary)/'tests'),'--junitxml',str(result_file)],env=env)
        if result_file.exists():
            suite=ET.parse(result_file).find('testsuite')
            print(json.dumps({'checks':dict(suite.attrib),'exit_code':result,'partial':args.without_dom},ensure_ascii=False),flush=True)
        return result

if __name__=='__main__':raise SystemExit(main())
