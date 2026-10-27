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

    env = {'secrets': secrets, 'safe_callback_answer': acknowledge,
           'FSMStates': SimpleNamespace(wait_for_receipt=SimpleNamespace(state='receipt'))}
    exec(compile(ast.fix_missing_locations(module), 'purchase-cancel-handler', 'exec'), env)

    class State:
        def __init__(self, status='receipt'):
            self.status, self.cleared = status, 0
        async def get_data(self):
            return {'cancel_token': 'new-session'}
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
            await env['cancel_receipt_wait'](SimpleNamespace(data=data, message=Message()), state)
            assert state.cleared == 0
        await env['cancel_receipt_wait'](SimpleNamespace(data='purchase:cancel:new-session', message=Message()), state)
        assert state.cleared == 1
        completed = State(status='another-state')
        await env['cancel_receipt_wait'](SimpleNamespace(data='purchase:cancel:new-session', message=Message()), completed)
        assert completed.cleared == 0

    asyncio.run(exercise())
