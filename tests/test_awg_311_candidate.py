import ast
import json
import unittest
from pathlib import Path
from typing import Any


def load_payload(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_editable_client_payload", "_int", "_bool", "_client_credential_id", "_string_list", "_reverse_object"}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    namespace = {"Any": Any, "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_editable_client_payload"]


class CompatibilityTest(unittest.TestCase):
    def test_real_update_payload_preserves_awg_and_renewal(self):
        payload = load_payload(Path(__file__).resolve().parents[1] / "app" / "services" / "xui_api.py")
        for value in ("15-25", 25, 0):
            with self.subTest(keepAlive=value):
                result = payload({"uuid": "test-id", "email": "test", "keepAlive": value, "resetWeekday": 4, "allowedIPs": "10.8.1.2/32"})
                self.assertEqual(result["keepAlive"], value)
                self.assertEqual(result["resetWeekday"], 4)
                self.assertEqual(result["allowedIPs"], ["10.8.1.2/32"])

    def test_keepalive_omission_stays_omitted(self):
        payload = load_payload(Path(__file__).resolve().parents[1] / "app" / "services" / "xui_api.py")
        self.assertNotIn("keepAlive", payload({"uuid": "test-id"}))


if __name__ == "__main__":
    unittest.main()

