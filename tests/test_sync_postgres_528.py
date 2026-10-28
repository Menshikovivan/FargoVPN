from test_billing_concurrency_pg import app, sample
import pytest

@pytest.mark.parametrize('old,panel', [(1800000000000,0),(0,1800000000000),(1800000000000,1900000000000)])
def test_live_postgres_source_of_truth(app,sample,old,panel):
    ids,sql=sample
    tg=ids[0]
    email=f'synthetic-{tg}@example.test'
    uuid=f'uuid-{tg}'
    sql('UPDATE users SET email=?, uuid=?, expiry_time=?, enable=1 WHERE tg_id=?',(email,uuid,old,tg))
    xui=app['xui_api']
    xui.sync_snapshot_to_db({'clients':[{'tg_id':tg,'email':email,'uuid':uuid,'expiry_time':panel,'enable':True,'up':0,'down':0,'total':0,'sub_id':'s'}]})
    row=sql('SELECT expiry_time FROM users WHERE tg_id=?',(tg,))[0]
    assert int(row[0])==panel
