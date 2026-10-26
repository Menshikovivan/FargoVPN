"""Render actual cabinet HTML with canonical subscription helpers."""
import ast
import html
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
APP=Path(__file__).resolve().parents[1]/'app'
NOW=1_700_000_000_000

def render(user,app=APP):
    helpers={}
    tree=ast.parse((app/'services/billing.py').read_text(encoding='utf-8'))
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'has_subscription','subscription_label','subscription_status_tone'}]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'canonical-billing','exec'),helpers)
    billing=NS(**{n.name:helpers[n.name] for n in nodes},telegram_identity_label=lambda tid,*args:f'ID {tid}')
    env={'Any':object,'html':html,'time':NS(time=lambda:NOW/1000),'billing':billing,'user_subscription_label':billing.subscription_label,
         'panel_from_timestamp':datetime.fromtimestamp,'remaining_label':lambda *a:'30 дн.','bytes_to_gb':lambda _:0,
         'cabinet_service':NS(public_username=lambda *a:''),'_cabinet_bot_username':lambda:'synthetic_bot',
         'referral_codes':NS(ensure_referral_code=lambda *a:'1234'),'config':NS(DB_PATH='unused',SERVICE_NAME='Synthetic'),
         'public_path':lambda p:'/qa-panel'+p,'update_manager':NS(current_version=lambda:'test')}
    fn=next(n for n in ast.parse((app/'webapp.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='_cabinet_markup')
    brand=next(n for n in ast.parse((app/'webapp.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='_brand_mark')
    exec(compile(ast.Module(body=[brand,fn],type_ignores=[]),'actual-cabinet','exec'),env)
    return env['_cabinet_markup']('synthetic',user,'https://synthetic.invalid/sub'),billing

@pytest.mark.parametrize('family,registration,credentials,enabled,expiry,label,tone',[
 (True,'active',False,True,0,'Семейная · без подписки','warn'),
 (True,'awaiting_invite',False,True,0,'Не зарегистрирован','warn'),
 (True,'active',True,False,0,'Семейная · доступ отключён','bad'),
 (True,'active',True,True,NOW-1,'Семейная · подписка истекла','warn'),
 (True,'active',True,True,0,'Семейная · бессрочная подписка','good'),
 (True,'active',True,True,NOW+1000,'Семейная · подписка активна','good'),
 (False,'active',False,True,0,'Без подписки','warn'),
 (False,'awaiting_invite',False,True,0,'Не зарегистрирован','warn'),
 (False,'banned',True,True,0,'Заблокирован','bad'),
 (False,'active',True,False,0,'Доступ отключён','bad'),
 (False,'active',True,True,NOW-1,'Подписка истекла','warn'),
 (False,'active',True,True,0,'Бессрочная подписка','good'),
 (False,'active',True,True,NOW+1000,'Подписка активна','good'),
])
def test_rendered_badge_matches_canonical_status(family,registration,credentials,enabled,expiry,label,tone):
    user=dict(tg_id=102,username='',registration_status=registration,enable=enabled,expiry_time=expiry,
              uuid='uuid' if credentials else '',email='',sub_id='',family_subscription=family,
              family_owner_tg_id=101,family_owner_has_subscription=credentials,
              family_owner_expiry_time=expiry,family_owner_enable=enabled)
    output,billing=render(user)
    assert billing.subscription_label(user,NOW)==label
    assert f'<span class="badge {tone}">{label}</span>' in output
    if not credentials:assert '∞' not in output and '<b>Бессрочно</b>' not in output
