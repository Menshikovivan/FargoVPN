"""Sequence changes survive transaction rollback; run only on an explicit lab DB."""
import ast
import os
from pathlib import Path
import uuid
import pytest

@pytest.mark.skipif(not os.getenv('FARGOVPN_TEST_BILLING_DSN'), reason='requires explicit disposable PostgreSQL DSN')
def test_restore_sequence_reset_cannot_rewind_after_transaction_rollback():
    import psycopg
    dsn = os.environ['FARGOVPN_TEST_BILLING_DSN'].replace('postgresql+psycopg://','postgresql://')
    source = Path(__file__).resolve().parents[1] / 'app' / 'restore_manager.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    ns = {'Any':object}
    for node in tree.body:
        if isinstance(node,ast.FunctionDef) and node.name in {'_sequence_names','_reset_table_sequences'}:
            exec(compile(ast.Module(body=[node],type_ignores=[]),'restore_sequence','exec'),ns)
    table = 'restore_sequence_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(f'CREATE TABLE public."{table}" (id BIGSERIAL PRIMARY KEY)')
        try:
            conn.execute(f'INSERT INTO public."{table}" DEFAULT VALUES')
            conn.execute(f'INSERT INTO public."{table}" DEFAULT VALUES')
            conn.execute('BEGIN')
            conn.execute(f'DELETE FROM public."{table}"')
            conn.execute(f'INSERT INTO public."{table}"(id) VALUES(1)')
            ns['_reset_table_sequences'](conn,table,['id'])
            conn.execute('ROLLBACK')
            # Original bug rewound to 1 despite rollback restoring row id=2.
            row = conn.execute(f'INSERT INTO public."{table}" DEFAULT VALUES RETURNING id').fetchone()
            assert row[0] > 2
            conn.execute(f'DELETE FROM public."{table}"')
            ns['_reset_table_sequences'](conn,table,['id'])
            assert conn.execute(f'INSERT INTO public."{table}" DEFAULT VALUES RETURNING id').fetchone()[0] > row[0]
        finally:
            conn.execute('ROLLBACK')
            conn.execute(f'DROP TABLE public."{table}"')
