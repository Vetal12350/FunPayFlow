"""Owner-facing NodeForm failure hints must be fixed-code only, never raw details."""

import pytest

from funpayflow.node_form import NodeFormError
from funpayflow.telegram_node_fields import safe_error_text


@pytest.mark.parametrize("reason", ["HTTP_AUTH", "URL_NODE", "FORM_COUNT", "APP_USER_TYPE", "REQUEST_FAILED"])
def test_owner_form_error_shows_allowlisted_code_only(reason):
    error = NodeFormError(reason, http_status=403, url_category="auth")
    error.args = ("secret synthetic response text",)
    shown = safe_error_text(error)
    assert shown == "Не удалось получить актуальные параметры раздела FunPay.\nКод диагностики: " + reason
    assert "403" not in shown
    assert "auth" not in shown
    assert "secret" not in shown


def test_unknown_reason_cannot_inject_private_text():
    error = NodeFormError("PRIVATE_SESSION_VALUE", secret="sensitive content")
    assert safe_error_text(error).endswith("Код диагностики: UNEXPECTED")
    assert "PRIVATE" not in safe_error_text(error)
    assert "sensitive" not in safe_error_text(error)


def test_generic_error_does_not_leak_untyped_exceptions():
    assert safe_error_text(ValueError("personal data")) == "Не удалось получить актуальные параметры раздела FunPay."
    assert safe_error_text() == "Не удалось получить актуальные параметры раздела FunPay."
