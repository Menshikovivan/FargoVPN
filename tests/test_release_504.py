"""Backup size threshold: real byte boundaries, no Telegram transport."""
from pathlib import Path
from test_release_501 import case


def test_backup_45mb_threshold_and_exact_rejoin(tmp_path):
    case(tmp_path,r'''
import backup,hashlib
limit=45_000_000
assert backup.BACKUP_SINGLE_FILE_BYTES==limit
for size in (0,1,limit-1,limit):
    source=Path('backup-'+str(size)+'.tar.gz')
    with source.open('wb') as handle:
        if size:handle.seek(size-1);handle.write(b'X')
    parts=backup.split_for_telegram(source,Path('small-parts'),1_000_000)
    assert parts==[source] and not Path('small-parts').exists()
source=Path('large.tar.gz')
with source.open('wb') as handle:handle.seek(limit);handle.write(b'X')
parts=backup.split_for_telegram(source,Path('large-parts'),limit)
assert len(parts)==2 and all(p.stat().st_size<=limit for p in parts)
expected=hashlib.sha256(source.read_bytes()).hexdigest();joined=hashlib.sha256()
for part in parts:
    assert part.stat().st_mode&0o777==0o600
    joined.update(part.read_bytes())
assert joined.hexdigest()==expected
# Configured lower chunk size still applies to large files, not small files.
parts=backup.split_for_telegram(source,Path('ten-mb'),10_000_000)
assert len(parts)==5 and all(p.stat().st_size<=10_000_000 for p in parts)
''')


def test_copy_reader_preserves_escaped_fields_and_empty_tables(tmp_path):
    import importlib.util
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('backup_fixture',root/'tests/qa/backup_fixture.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sql=tmp_path/'test.sql'
    sql.write_text('CREATE TABLE public.users (\n    tg_id bigint NOT NULL,\n    text text,\n    enable boolean\n);\nALTER TABLE ONLY public.users\n    ADD CONSTRAINT users_pkey PRIMARY KEY (tg_id);\nCOPY public.users (tg_id, text, enable) FROM stdin;\n1\tHello\\tworld\\nline\\\\end\tt\n2\t\\N\tf\n\\.\nCREATE TABLE public.empty (\n    id bigint\n);\nCOPY public.empty (id) FROM stdin;\n\\.\n')
    dest=tmp_path/'data.sqlite';counts=module.build_fixture(sql,dest)
    assert counts=={'users':2,'empty':0}
    import sqlite3
    c=sqlite3.connect(dest)
    assert c.execute('SELECT text,enable FROM users WHERE tg_id=1').fetchone()==('Hello\tworld\nline\\end',1)
    assert c.execute('SELECT text,enable FROM users WHERE tg_id=2').fetchone()==(None,0)
