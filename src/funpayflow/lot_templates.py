"""Conservative own-lot drafts; publication awaits a verified creation schema.

FunPayAPI 1.1.0 documents editing arbitrary forms, not creation identity or
delivery-mode semantics. Neither changing LotFields.lot_id nor guessing a hidden
field can establish creation. This module deliberately sends no modifying HTTP.
"""

from decimal import Decimal, InvalidOperation

from FunPayAPI.common.enums import SubCategoryTypes

from .runtime_control import automatic_action_gate
from .state import StateError


class LotTemplateError(RuntimeError):
    """A fixed, safe message that may be shown in Telegram."""


PUBLICATION_BLOCKER = (
    "Публикация недоступна: схема создания нового лота и ручной выдачи "
    "FunPay ещё не подтверждена. Черновик сохранён; запрос не отправлен."
)

TEXT_FIELDS = {
    "title_ru": "fields[summary][ru]",
    "title_en": "fields[summary][en]",
    "description_ru": "fields[desc][ru]",
    "description_en": "fields[desc][en]",
}
EDITABLE_FIELDS = (*TEXT_FIELDS, "price", "amount")
# These metadata names are discarded, never copied into a creation payload.
# Their presence is NOT evidence of a supported creation/delivery contract.
_DISCARDED_METADATA = {"offer", "node", "offer_id", "node_id", "csrf_token", "location",
                       "form_created_at"}
_BASIC_FIELDS = {*TEXT_FIELDS.values(), "price", "amount", "active",
                 "deactivate_after_sale", "deactivate_after_sale[]"}


class LotTemplateService:
    """Synchronous API suitable for asyncio.to_thread; no network in __init__."""

    def __init__(self, client, late_check=None):
        self.client = client
        self.store = client.review_state
        self.late_check = late_check or (lambda: False)

    def _account_id(self):
        account_id = getattr(self.client.account, "id", None)
        if type(account_id) is not int or account_id <= 0:
            raise LotTemplateError("Аккаунт FunPay недоступен.")
        return account_id

    def _own_lots(self):
        try:
            with self.client._account_lock:
                account_id = self._account_id()
                profile = self.client.account.get_user(account_id)
                if getattr(profile, "id", None) != account_id:
                    raise LotTemplateError("Не удалось проверить владельца лотов.")
                return list(profile.get_lots())
        except (LotTemplateError, StateError):
            raise
        except Exception:
            raise LotTemplateError("Не удалось прочитать собственные лоты FunPay.") from None

    @staticmethod
    def _is_common(lot):
        return getattr(getattr(lot, "subcategory", None), "type", None) is SubCategoryTypes.COMMON

    def discover(self):
        """COMMON candidates only; no category is claimed publishable."""
        return [{"source_id": lot.id, "title": str(lot.title or "")[:200],
                 "subcategory_id": lot.subcategory.id,
                 "subcategory": str(getattr(lot.subcategory, "name", ""))[:200],
                 "publish_supported": False}
                for lot in self._own_lots()
                if self._is_common(lot) and type(lot.id) is int and lot.id > 0]

    @staticmethod
    def _business_fields(fields):
        """Reject unreviewed category/goods/credential fields before persistence."""
        raw = fields.fields
        if not isinstance(raw, dict) or set(raw) - _BASIC_FIELDS - _DISCARDED_METADATA:
            raise LotTemplateError(
                "Форма содержит неподдерживаемые поля категории или автоматической выдачи."
            )
        result = {key: raw.get(name, "") for key, name in TEXT_FIELDS.items()}
        result.update(price=str(raw.get("price", "")), amount=str(raw.get("amount", "")))
        if any(type(value) is not str for value in result.values()):
            raise LotTemplateError("Неподдерживаемый формат полей лота.")
        return result

    def create_draft(self, owner_id, source_id):
        if type(source_id) is not int or source_id <= 0:
            raise LotTemplateError("Некорректный номер исходного лота.")
        with self.client._account_lock:
            lots = self._own_lots()
            source = next((lot for lot in lots if lot.id == source_id), None)
            if source is None or not self._is_common(source):
                raise LotTemplateError("Выберите собственный лот из обычной категории COMMON.")
            try:
                payload = self._business_fields(self.client.account.get_lot_fields(source_id))
            except LotTemplateError:
                raise
            except Exception:
                raise LotTemplateError("Не удалось прочитать форму исходного лота.") from None
            self.validate(payload)
            row = self.store.create_lot_template_draft(
                str(owner_id), self._account_id(), source_id, source.subcategory.id,
                payload, [lot.id for lot in lots if type(lot.id) is int],
            )
        return self._summary(row)

    def _get(self, owner_id, draft_id):
        row = self.store.get_lot_template_draft(str(owner_id), draft_id)
        if row is None or row["account_id"] != self._account_id():
            raise LotTemplateError("Черновик не найден для этого пользователя и аккаунта.")
        return row

    @staticmethod
    def _summary(row):
        return {key: row[key] for key in (
            "draft_id", "source_id", "subcategory_id", "state", "revision", "payload"
        )} | {"publish_supported": False, "publication_blocker": PUBLICATION_BLOCKER}

    def get_draft(self, owner_id, draft_id):
        return self._summary(self._get(owner_id, draft_id))

    def list_drafts(self, owner_id):
        return [self._summary(row) for row in self.store.list_lot_template_drafts(
            str(owner_id), self._account_id())]

    @staticmethod
    def validate(payload):
        if set(payload) != set(EDITABLE_FIELDS):
            raise LotTemplateError("Неподдерживаемые поля черновика.")
        for name in TEXT_FIELDS:
            value = payload[name]
            maximum = 500 if name.startswith("title_") else 20000
            if (type(value) is not str or len(value) > maximum
                    or any(ord(char) < 32 and char not in "\n\r\t" for char in value)):
                raise LotTemplateError("Некорректная длина или формат текста лота.")
        if not (payload["title_ru"].strip() or payload["title_en"].strip()):
            raise LotTemplateError("Укажите название хотя бы на одном языке.")
        if not (payload["description_ru"].strip() or payload["description_en"].strip()):
            raise LotTemplateError("Укажите описание хотя бы на одном языке.")
        try:
            raw_price = payload["price"]
            if type(raw_price) is not str or len(raw_price) > 20:
                raise ValueError
            price = Decimal(raw_price)
            if not price.is_finite() or price <= 0 or price > Decimal("1000000000"):
                raise ValueError
            raw_amount = payload["amount"]
            if (type(raw_amount) is not str or not raw_amount.isascii()
                    or not raw_amount.isdecimal() or len(raw_amount) > 9
                    or not 1 <= int(raw_amount) <= 100000000):
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise LotTemplateError("Укажите положительную цену и целое количество от 1 до 100000000.") from None
        return True

    def edit_draft(self, owner_id, draft_id, field, value):
        row = self._get(owner_id, draft_id)
        if field not in EDITABLE_FIELDS:
            raise LotTemplateError("Это поле черновика нельзя редактировать.")
        payload = dict(row["payload"])
        payload[field] = value
        self.validate(payload)
        if not self.store.update_lot_template_draft(
                str(owner_id), draft_id, row["revision"], payload):
            raise LotTemplateError("Черновик изменён или больше недоступен для редактирования.")
        return self.get_draft(owner_id, draft_id)

    def preview(self, owner_id, draft_id):
        row = self._get(owner_id, draft_id)
        self.validate(row["payload"])
        if not self.store.update_lot_template_draft(
                str(owner_id), draft_id, row["revision"], row["payload"], preview=True):
            raise LotTemplateError("Черновик больше недоступен для предпросмотра.")
        return self.get_draft(owner_id, draft_id)

    def cancel(self, owner_id, draft_id):
        self._get(owner_id, draft_id)
        if not self.store.cancel_lot_template_draft(str(owner_id), draft_id):
            raise LotTemplateError("Нельзя отменить завершённый или уже отправленный черновик.")
        return self.get_draft(owner_id, draft_id)

    def confirm(self, owner_id, draft_id, revision=None):
        row = self._get(owner_id, draft_id)
        if row["state"] in ("claimed", "ambiguous"):
            raise LotTemplateError("Результат неизвестен. Повторная отправка запрещена; нужна проверка.")
        if row["state"] != "previewed" or (revision is not None and revision != row["revision"]):
            raise LotTemplateError("Сначала откройте актуальный предпросмотр черновика.")
        self.validate(row["payload"])
        with self.client._account_lock, automatic_action_gate() as allowed:
            if not allowed or not self.late_check():
                raise LotTemplateError("Действие заблокировано текущим состоянием бота / SAFE_MODE.")
            # No claim or modifying HTTP until a real creation form contract is verified.
            raise LotTemplateError(PUBLICATION_BLOCKER)

    def verify(self, owner_id, draft_id):
        row = self._get(owner_id, draft_id)
        # No supported publication can originate here yet. Never equate an unrelated
        # matching lot with proof of a write, or turn UNKNOWN back into a draft.
        return self._summary(row) | {"verification": "unsupported_schema"}
