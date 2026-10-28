import importlib.util
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('expiry_snapshots',Path(__file__).resolve().parents[1]/'app'/'expiry_snapshots.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


def snapshot(expiry=1800000000000):
    return {'stale':False,'clients':[{'uuid':'id-1','email':'a@example.test','expiry_time':expiry,'secret':'should-not-be-saved'}]}


def test_snapshot_explicit_two_reads_private_and_minimal(tmp_path):
    calls=[]
    def fetch():calls.append(1);return snapshot()
    result=s.create_snapshot(tmp_path/'private',fetch)
    assert len(calls)==2 and result['count']==1
    path=next((tmp_path/'private').glob('snapshot-*.json'))
    data=json.loads(path.read_text()); assert data['clients']==[{'uuid':'id-1','email':'a@example.test','expiry_time':1800000000000}]
    assert 'secret' not in path.read_text()
    if os.name!='nt': assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_manual_comparison_detects_finite_to_unlimited_without_writing_3x(tmp_path):
    s.create_snapshot(tmp_path,lambda:snapshot())
    result=s.compare_latest(tmp_path,lambda:snapshot(0))
    assert result['counts']['finite_to_zero']==1 and result['counts']['changed']==1
    assert result['changes'][0]['after']==0


def test_existing_snapshots_not_overwritten_on_new_capture(tmp_path):
    s.create_snapshot(tmp_path,lambda:snapshot()); files=list(tmp_path.glob('snapshot-*.json'))
    before=files[0].read_bytes();s.create_snapshot(tmp_path,lambda:snapshot(1))
    assert files[0].read_bytes()==before and len(list(tmp_path.glob('snapshot-*.json')))==2


def test_changed_between_reads_never_creates_snapshot(tmp_path):
    values=iter([snapshot(1),snapshot(2)])
    with pytest.raises(ValueError,match='меняются'): s.create_snapshot(tmp_path,lambda:next(values))
    assert not list(tmp_path.glob('snapshot-*.json'))


@pytest.mark.parametrize('bad',[{'stale':True,'clients':[]},{'clients':[]},{'clients':[{'uuid':'x','email':'x','expiry_time':-1}]},{'clients':[{'uuid':'x','email':'x','expiry_time':True}]}])
def test_invalid_or_deferred_snapshot_is_rejected(tmp_path,bad):
    with pytest.raises(ValueError): s.create_snapshot(tmp_path,lambda:bad)
    assert not list(tmp_path.glob('snapshot-*.json'))


def test_ambiguous_identity_not_assumed(tmp_path):
    rows=[{'uuid':'A','email':'one','expiry_time':1},{'uuid':'a','email':'two','expiry_time':1}]
    with pytest.raises(ValueError,match='неоднозначные'):s.create_snapshot(tmp_path,lambda:{'clients':rows})


def test_write_failure_never_replaces_previous_snapshot(tmp_path,monkeypatch):
    s.create_snapshot(tmp_path,lambda:snapshot()); existing=next(tmp_path.glob('snapshot-*.json')); before=existing.read_bytes()
    monkeypatch.setattr(s.os,'fsync',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):s.create_snapshot(tmp_path,lambda:snapshot(0))
    assert existing.read_bytes()==before and len(list(tmp_path.glob('snapshot-*.json')))==1


def test_concurrent_snapshots_are_separate_and_valid(tmp_path):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda n:s.create_snapshot(tmp_path,lambda:snapshot(n)),range(1,4)))
    files=list(tmp_path.glob('snapshot-*.json'))
    assert len(files)==3 and len(results)==3
    assert {json.loads(x.read_text())['clients'][0]['expiry_time'] for x in files}=={1,2,3}


def test_preupdate_snapshot_is_separate_from_confirmed_baseline(tmp_path):
    s.create_snapshot(tmp_path,lambda:snapshot(100))
    s.create_snapshot(tmp_path,lambda:snapshot(0),pre_update=True)
    report=s.compare_latest(tmp_path,lambda:snapshot(0))
    assert report['counts']['finite_to_zero']==1
    data=json.loads(next(tmp_path.glob('preupdate-*.json')).read_text())
    assert data['kind']=='preupdate' and data['clients'][0]['expiry_time']==0


def test_empty_snapshot_requires_explicit_proof_empty_database(tmp_path):
    with pytest.raises(ValueError): s.create_snapshot(tmp_path,lambda:{'clients':[]},pre_update=True)
    result=s.create_snapshot(tmp_path,lambda:{'clients':[]},pre_update=True,allow_empty=True)
    assert result['count']==0


def test_preupdate_failure_does_not_overwrite_manual_snapshot(tmp_path):
    s.create_snapshot(tmp_path,lambda:snapshot())
    old=next(tmp_path.glob('snapshot-*.json')); data=old.read_bytes()
    with pytest.raises(ValueError): s.create_snapshot(tmp_path,lambda:{'stale':True,'clients':[]},pre_update=True)
    assert old.read_bytes()==data and not list(tmp_path.glob('preupdate-*.json'))


def test_retention_limit_never_deletes_existing_snapshots(tmp_path):
    for i in range(128):(tmp_path/('snapshot-%04d.json'%i)).write_text('{}')
    with pytest.raises(s.SnapshotLimitError): s.create_snapshot(tmp_path,lambda:snapshot(),pre_update=True)
    assert len(list(tmp_path.glob('snapshot-*.json')))==128


@pytest.mark.skipif(os.name=='nt',reason='POSIX snapshot ownership and no-follow semantics')
def test_symlinked_directory_rejected(tmp_path):
    safe=tmp_path/'safe';safe.mkdir(); link=tmp_path/'link';link.symlink_to(safe,target_is_directory=True)
    with pytest.raises(ValueError):s.create_snapshot(link,lambda:snapshot())
    assert not list(safe.glob('snapshot-*.json'))


@pytest.mark.skipif(os.name=='nt',reason='POSIX file permissions')
def test_insecure_snapshot_is_never_read(tmp_path):
    s.create_snapshot(tmp_path,lambda:snapshot()); path=next(tmp_path.glob('snapshot-*.json'));path.chmod(0o644)
    with pytest.raises(ValueError,match='права'):s.compare_latest(tmp_path,lambda:snapshot())
