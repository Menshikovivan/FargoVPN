"""Manual, read-only 3x-ui subscription snapshots for administrator review.

No remote writes or automatic restore are performed. Snapshots are private and
not a substitute for the full PostgreSQL/3x-ui backup.
"""
from __future__ import annotations
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json, os, secrets, stat


class SnapshotLimitError(ValueError):
    """Manual retention must be reviewed rather than silently deleting backups."""


def _validated_snapshot(snapshot, *, allow_empty=False):
    if not isinstance(snapshot,dict) or snapshot.get('stale') or snapshot.get('error'):
        raise ValueError('Нет подтверждённых свежих данных 3x-ui')
    rows=snapshot.get('clients')
    if not isinstance(rows,list) or (not rows and not allow_empty):
        raise ValueError('Пустой или некорректный список клиентов: снимок не создан')
    if len(rows)>50000: raise ValueError('Слишком много клиентов для снимка')
    clean=[]; seen_uuid=set(); seen_email=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError('Повреждённый список клиентов 3x-ui')
        uid=str(row.get('uuid') or '').strip(); email=str(row.get('email') or '').strip()
        expiry=row.get('expiry_time')
        if not uid or not email or len(uid)>512 or len(email)>512 or isinstance(expiry,bool) or not isinstance(expiry,int) or expiry<0 or expiry>253402300799000:
            raise ValueError('Снимок невозможен: отсутствует идентификатор или есть отложенная подписка')
        if uid.casefold() in seen_uuid or email.casefold() in seen_email:
            raise ValueError('Обнаружены неоднозначные идентификаторы 3x-ui')
        seen_uuid.add(uid.casefold()); seen_email.add(email.casefold())
        clean.append({'uuid':uid,'email':email,'expiry_time':expiry})
    return sorted(clean,key=lambda r:r['uuid'].casefold())


def _check_parents(directory):
    if os.name=='nt': return
    directory=directory.absolute()
    for p in (directory,*directory.parents):
        try: st=p.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode): raise ValueError('Небезопасный путь снимков')
        if st.st_uid not in {0,os.geteuid()}: raise ValueError('Недоверенный владелец каталога снимков')
        if st.st_mode & 0o022 and not (st.st_uid==0 and st.st_mode & stat.S_ISVTX):
            raise ValueError('Недоверенные права каталога снимков')


@contextmanager
def _locked(directory):
    directory=Path(directory)
    _check_parents(directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    _check_parents(directory)
    if directory.is_symlink() or not directory.is_dir(): raise ValueError('Каталог снимков недоступен')
    if os.name!='nt' and directory.stat().st_uid != os.geteuid(): raise ValueError('Недоверенный владелец каталога снимков')
    if os.name!='nt': os.chmod(directory,0o700)
    flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(directory/'.lock',flags,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('Некорректный файл блокировки')
        if os.name!='nt' and os.fstat(fd).st_uid != os.geteuid(): raise ValueError('Недоверенный владелец блокировки')
        if os.name!='nt': os.fchmod(fd,0o600)
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0: os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def _files(directory, prefix='snapshot-'):
    return sorted(p for p in directory.glob(prefix+'*.json') if p.is_file() and not p.is_symlink())


def create_snapshot(directory, fetch, *, pre_update=False, allow_empty=False):
    """Capture only upon explicit action and two identical read-only snapshots."""
    with _locked(directory) as folder:
        if len(_files(folder))+len(_files(folder,'preupdate-'))>=128: raise SnapshotLimitError('Достигнут предел снимков; требуется ручная уборка')
        first=_validated_snapshot(fetch(),allow_empty=allow_empty); second=_validated_snapshot(fetch(),allow_empty=allow_empty)
        if first!=second: raise ValueError('Данные 3x-ui меняются; повторите снимок позже')
        ts=datetime.now(timezone.utc)
        kind='preupdate' if pre_update else 'manual_confirmed'
        name=('preupdate-' if pre_update else 'snapshot-')+ts.strftime('%Y%m%dT%H%M%S%fZ')+'-'+secrets.token_hex(6)+'.json'
        payload={'created_utc':ts.isoformat(),'clients':first,'schema':1,'kind':kind}
        data=json.dumps(payload,ensure_ascii=True,separators=(',',':')).encode('utf8')
        path=folder/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(data);f.flush();os.fsync(f.fileno())
        except Exception:
            # Invalid incomplete snapshots must never be selected as the latest.
            path.unlink(missing_ok=True);raise
        return {'created_utc':payload['created_utc'],'count':len(first)}


def compare_latest(directory, fetch, *, source='manual'):
    if source not in {'manual','preupdate'}: raise ValueError('Некорректный тип снимка')
    with _locked(directory) as folder:
        files=_files(folder, 'preupdate-' if source=='preupdate' else 'snapshot-')
        if not files: return {'available':False}
        path=files[-1]
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'r',encoding='utf8') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size>16*1024*1024: raise ValueError('Снимок недоступен или слишком большой')
            if os.name!='nt' and (st.st_uid!=os.geteuid() or st.st_mode & 0o077): raise ValueError('Небезопасные права снимка')
            saved=json.load(f)
        if not isinstance(saved,dict) or saved.get('schema')!=1: raise ValueError('Снимок повреждён')
        if saved.get('kind') != ('preupdate' if source=='preupdate' else 'manual_confirmed'):
            raise ValueError('Тип снимка не подтверждён')
        before=_validated_snapshot({'clients':saved.get('clients')}, allow_empty=(source=='preupdate'))
        after=_validated_snapshot(fetch(),allow_empty=True)
        old={r['uuid'].casefold():r for r in before}; new={r['uuid'].casefold():r for r in after}
        report=[]; count=Counter()
        for uid,row in old.items():
            fresh=new.get(uid)
            if not fresh: count['missing']+=1;continue
            if row['email'].casefold()!=fresh['email'].casefold(): count['identity_changed']+=1;continue
            if row['expiry_time']!=fresh['expiry_time']:
                count['changed']+=1
                if row['expiry_time']>0 and fresh['expiry_time']==0: count['finite_to_zero']+=1
                report.append({'email':row['email'],'before':row['expiry_time'],'after':fresh['expiry_time']})
            else: count['same']+=1
        count['new']=len(set(new)-set(old))
        return {'available':True,'created_utc':str(saved.get('created_utc') or ''),'counts':dict(count),'changes':report, 'current_empty':not after}
