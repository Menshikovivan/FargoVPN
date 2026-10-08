#!/usr/bin/env python3
"""One-command regression runner; external services are isolated, QA DB required."""
import argparse,json,os,pathlib,subprocess,sys,tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--old',type=pathlib.Path);p.add_argument('--current',type=pathlib.Path);p.add_argument('--output',type=pathlib.Path,default=ROOT/'work'/'regression');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
 if not os.getenv('QA_DATABASE_URL'):raise SystemExit('Set QA_DATABASE_URL to a separate loopback PostgreSQL database ending in _qa. Production DSNs are refused.')
 from importlib.util import spec_from_file_location,module_from_spec
 spec=spec_from_file_location('seed',ROOT/'tests/qa/seed_regression.py');seed=module_from_spec(spec);spec.loader.exec_module(seed)
 data=a.output/'fixture.sqlite'
 if data.exists():raise SystemExit('Choose a new --output directory; never overwrite an existing test fixture.')
 seed.seed(data,1000)
 def run(script,*args):
  subprocess.run([sys.executable,str(ROOT/'tests/qa'/script),*map(str,args)],check=True)
 for label,root in [('old',a.old),('fixed',ROOT)]:
  if root is None:continue
  render=a.output/('render-'+label);run('render_regression.py',root,render,data)
  run('browser_regression.py',root,render,a.output/('browser-'+label+'.json'),*(['strict'] if label=='fixed' else []))
  run('postgres_regression.py',root,a.output/('postgres-'+label+'.json'))
 run('diagnostic_regression.py',ROOT)
 run('collector_regression.py')
 if a.current:
  render=a.output/'render-current';run('render_regression.py',a.current,render,data)
  r=subprocess.run([sys.executable,str(ROOT/'tests/qa/browser_regression.py'),str(a.current),str(render),str(a.output/'browser-current-strict.json'),'strict','--probe'])
  if r.returncode!=1:raise SystemExit('Expected CSP failure was not reproduced; inspect report')
 print('Reports:',a.output.resolve())
if __name__=='__main__':main()
