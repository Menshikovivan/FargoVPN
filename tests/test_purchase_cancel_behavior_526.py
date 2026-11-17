"""Exercise the real cancel handler with isolated FSM/callback doubles."""
import ast
import asyncio
from pathlib import Path
import secrets
from types import SimpleNamespace


def test_only_current_purchase_cancel_clears_receipt_wait():
    source = (Path(__file__).resolve().parents[1] / 'app/main.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {'purchase_cancel_token_matches', 'cancel_receipt_wait'}
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for function in functions:
        function.decorator_list = []
    assert len(functions) == 2
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *functions], type_ignores=[])

    async def acknowledge(_):
        return None

    quote = {"cancel_token": "new-session"}

    def cancel(payer_id, token, _db_path):
        assert payer_id == 101 and token == "new-session"
        quote.clear()
        return True

    async def run_in_thread(function, *args):
        return function(*args)

    env = {'secrets': secrets, 'safe_callback_answer': acknowledge,
           'asyncio': SimpleNamespace(to_thread=run_in_thread),
           'config': SimpleNamespace(DB_PATH='test'),
           'get_purchase_quote_sync': lambda *_: dict(quote),
           'cancel_purchase_quote_sync': cancel,
           'FSMStates': SimpleNamespace(wait_for_receipt=SimpleNamespace(state='receipt'))}
    exec(compile(ast.fix_missing_locations(module), 'purchase-cancel-handler', 'exec'), env)

    class State:
        def __init__(self, status='receipt'):
            self.status, self.cleared = status, 0
        async def get_state(self):
            return self.status
        async def clear(self):
            self.cleared += 1

    class Message:
        async def answer(self, text):
            self.text = text

    async def exercise():
        state = State()
        for data in ('purchase:cancel', 'purchase:cancel:old-session'):
            await env['cancel_receipt_wait'](SimpleNamespace(data=data, message=Message(), from_user=SimpleNamespace(id=101)), state)
            assert state.cleared == 0
        await env['cancel_receipt_wait'](SimpleNamespace(data='purchase:cancel:new-session', message=Message(), from_user=SimpleNamespace(id=101)), state)
        assert state.cleared == 1 and not quote
        quote['cancel_token'] = 'new-session'
        completed = State(status=None)
        await env['cancel_receipt_wait'](SimpleNamespace(data='purchase:cancel:new-session', message=Message(), from_user=SimpleNamespace(id=101)), completed)
        assert completed.cleared == 0 and not quote

    asyncio.run(exercise())
