"""Close notice says who confirmed the order; offline, main.py is parsed."""
import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import FunPayAPI

source = Path("src/funpayflow/main.py").read_text(encoding="utf-8")
nodes = [n for n in ast.parse(source).body
         if isinstance(n, ast.FunctionDef) and n.name == "_closed_by_admin"]
runtime = {"FunPayAPI": FunPayAPI, "FunPayClient": object,
           "logger": SimpleNamespace(warning=lambda *a: None)}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), runtime)
closed_by_admin = runtime["_closed_by_admin"]
TYPES = FunPayAPI.enums.MessageTypes


def client(history, chat_id=5):
    account = SimpleNamespace(
        get_chat_by_name=lambda name, make_request=False: SimpleNamespace(id=chat_id),
        get_chat_history=lambda cid, interlocutor_username=None: history)
    return SimpleNamespace(_account_lock=threading.RLock(), account=account)


def message(kind, text):
    return SimpleNamespace(type=kind, text=text)


class ClosedByAdminTests(unittest.TestCase):
    def test_admin_confirmation_of_this_order(self):
        history = [message(TYPES.ORDER_CONFIRMED_BY_ADMIN,
                           "Администратор Forest подтвердил успешное выполнение заказа "
                           "#P4ZH2WD2 и отправил деньги продавцу Seller.")]
        self.assertTrue(closed_by_admin(client(history), "Buyer", "P4ZH2WD2"))

    def test_buyer_confirmation_or_other_order(self):
        history = [message(TYPES.ORDER_CONFIRMED, "Покупатель Buyer подтвердил успешное "
                                                  "выполнение заказа #P4ZH2WD2"),
                   message(TYPES.ORDER_CONFIRMED_BY_ADMIN, "Администратор Forest подтвердил "
                                                           "успешное выполнение заказа #OTHER123")]
        self.assertFalse(closed_by_admin(client(history), "Buyer", "P4ZH2WD2"))

    def test_failure_is_unknown(self):
        broken = client([])
        def boom(*a, **k):
            raise RuntimeError("net")
        broken.account.get_chat_history = boom
        self.assertFalse(closed_by_admin(broken, "Buyer", "P4ZH2WD2"))
        self.assertFalse(closed_by_admin(client([]), None, "P4ZH2WD2"))


if __name__ == "__main__":
    unittest.main()
