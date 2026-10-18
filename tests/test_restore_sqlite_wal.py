import importlib.util
import logging
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'app/restore_manager.py'
spec = importlib.util.spec_from_file_location('restore_wal_test', path)
m = importlib.util.module_from_spec(spec)
with patch('logging.FileHandler', return_value=logging.NullHandler()):
    spec.loader.exec_module(m)

def rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute('select value from state').fetchall()

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    archive = root / 'archive.db'; current = root / 'xui/x-ui.db'; current.parent.mkdir()
    archived = sqlite3.connect(archive)
    archived.execute('pragma journal_mode=wal')
    archived.execute('pragma wal_autocheckpoint=0')
    archived.execute('create table state(value text)')
    archived.execute("insert into state values ('restored WAL data')"); archived.commit()
    live = sqlite3.connect(current)
    live.execute('pragma journal_mode=wal');live.execute('pragma wal_autocheckpoint=0')
    live.execute('create table state(value text)');live.execute("insert into state values ('original WAL data')");live.commit()
    # Emulate retained WAL files after a stopped process by copying them to another path.
    old = root / 'old.db'
    import shutil
    for suffix in ('', '-wal', '-shm'):
        shutil.copy2(Path(str(current)+suffix),Path(str(old)+suffix))
    live.close()
    for suffix in ('', '-wal', '-shm'):
        shutil.copy2(Path(str(old)+suffix),Path(str(current)+suffix))
    rollback = m._sqlite_xui_swap(root, archive, current, 'test')
    assert rows(current) == [('restored WAL data',)]
    assert rows(rollback) == [('original WAL data',)]
    assert m._sqlite_integrity(current) == 'ok'
    config_root = root / 'backup'; (config_root/'system/etc-x-ui').mkdir(parents=True)
    (config_root/'system/etc-x-ui/settings').write_text('restored')
    (config_root/'manifest.json').write_text('{"components":{"xray_config":false}}')
    cfg = lambda name, default=None: str(current) if name=='XUI_DB_PATH' else default
    with patch.object(m,'_cfg',side_effect=cfg):
        m._restore_system_configs(config_root,xui_target=current.parent)
    assert rows(current) == [('restored WAL data',)]
    assert rollback.is_file()
    assert not (current.parent/'settings').exists()  # Active archive configs are never applied
    m._sqlite_xui_rollback(current,rollback)
    assert rows(current) == [('original WAL data',)]
    archived.close()
print('WAL import, WAL rollback snapshot, configuration preservation, rollback: PASS')
