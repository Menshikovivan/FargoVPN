import importlib.util
import sys
import types
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))


def _worker(monkeypatch,tmp_path):
    manager=types.ModuleType('update_manager')
    class UpdateError(Exception):pass
    manager.UpdateError=UpdateError
    state={'job_id':'job1'}
    manager.update_log_path=lambda:tmp_path/'update.log'
    manager.read_status=lambda:dict(state)
    def status(s,**kw):state.update(kw,state=s)
    manager.write_status=status
    manager.read_job_manifest=lambda job:{'update':{'source':'manual','version':'5.3.0'}}
    manager.resolve_automatic_latest_update=lambda x:x
    manager.current_version=lambda:'5.2.9'
    manager.is_newer=lambda a,b:True
    manager.obtain_update_archive=lambda *a,**k:tmp_path/'archive'
    manager.stage_update=lambda *a,**k:(tmp_path,{'version':'5.3.0'})
    manager.installer_command=lambda root:(['synthetic-installer'],{})
    monkeypatch.setitem(sys.modules,'update_manager',manager)
    spec=importlib.util.spec_from_file_location('worker_test_530',ROOT/'app/update_worker.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module,state


def test_snapshot_failure_stops_before_installer_and_hides_secret(monkeypatch,tmp_path):
    worker,state=_worker(monkeypatch,tmp_path)
    worker._snapshot_before_upgrade=lambda:(_ for _ in ()).throw(ValueError('secret-password'))
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a,**k:pytest.fail('installer must never run'))
    assert worker.run('job1')==1 and state['state']=='failed'
    assert 'Защитный снимок' in state['error']
    assert 'secret-password' not in (tmp_path/'update.log').read_text(encoding='utf8')


def test_snapshot_success_precedes_install(monkeypatch,tmp_path):
    worker,state=_worker(monkeypatch,tmp_path);steps=[]
    worker._snapshot_before_upgrade=lambda:steps.append('snapshot')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a,**k:(steps.append('installer') or types.SimpleNamespace(returncode=0)))
    assert worker.run('job1')==0 and steps==['snapshot','installer'] and state['state']=='completed'


def test_snapshot_retention_limit_stops_with_clear_message(monkeypatch,tmp_path):
    worker,state=_worker(monkeypatch,tmp_path)
    worker._snapshot_before_upgrade=lambda:(_ for _ in ()).throw(worker.SnapshotLimitError('limit'))
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a,**k:pytest.fail('installer must not run'))
    assert worker.run('job1')==1 and '128 снимков' in state['error']


@pytest.mark.parametrize('existing,should_raise',[(0,False),(1,True)])
def test_preupdate_empty_3x_requires_empty_local_identity(monkeypatch,tmp_path,existing,should_raise):
    worker,_=_worker(monkeypatch,tmp_path)
    config=types.ModuleType('config');config.DB_PATH='unused';config.BACKUP_DIR=str(tmp_path)
    db=types.ModuleType('db')
    class Conn:
        def execute(self,*a):return self
        def fetchone(self):return (existing,)
        def close(self):pass
    db.connect=lambda *a,**k:Conn()
    spec=importlib.util.spec_from_file_location('expiry_snapshots',ROOT/'app/expiry_snapshots.py')
    snapshots=importlib.util.module_from_spec(spec);spec.loader.exec_module(snapshots)
    services=types.ModuleType('services');services.__path__=[]
    xui=types.ModuleType('services.xui_api');xui.fetch_snapshot_sync=lambda **k:{'clients':[],'stale':False}
    for key,value in [('config',config),('db',db),('expiry_snapshots',snapshots),('services',services),('services.xui_api',xui)]:monkeypatch.setitem(sys.modules,key,value)
    if should_raise:
        with pytest.raises(ValueError):worker._snapshot_before_upgrade()
        assert not list((tmp_path/'expiry-snapshots').glob('preupdate-*.json'))
    else:
        assert worker._snapshot_before_upgrade()['count']==0
