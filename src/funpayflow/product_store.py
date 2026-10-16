"""Scoped local product registry and durable, idempotent confirmation batches."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing


def normalize_game(value):
    return " ".join(str(value).split()).casefold()


def initialize_schema(connection):
    for table in ("product_templates", "game_routes", "product_drafts", "product_variants"):
        connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                           "id TEXT NOT NULL, owner_id TEXT NOT NULL, account_id INTEGER NOT NULL, "
                           "body TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL, "
                           "updated_at REAL NOT NULL, game_key TEXT, template_id TEXT, last_used REAL, "
                           "PRIMARY KEY(id,owner_id,account_id))")
        connection.execute(f"CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(owner_id, account_id)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS game_routes_unique_game "
                       "ON game_routes(owner_id,account_id,template_id,game_key)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS product_templates_unique_name "
                       "ON product_templates(owner_id,account_id,game_key) WHERE game_key IS NOT NULL")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS product_variants_unique_name "
                       "ON product_variants(owner_id,account_id,game_key)")
    connection.execute("CREATE TABLE IF NOT EXISTS product_batches (token TEXT PRIMARY KEY, "
                       "owner_id TEXT NOT NULL, account_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                       "status TEXT NOT NULL, body TEXT NOT NULL, result TEXT)")


class ProductStore:
    """Compose the existing receipt store; never open a separate database."""

    def __init__(self, store, owner_id, account_id):
        if type(account_id) is not int or account_id <= 0 or not str(owner_id).strip():
            raise ValueError("Некорректный владелец или аккаунт.")
        self.store, self.owner_id, self.account_id = store, str(owner_id), account_id

    def _scope(self):
        return self.owner_id, self.account_id

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        body = json.loads(row[3])
        body.update(id=row[0], revision=row[4], created_at=row[5], updated_at=row[6])
        return body

    def _get(self, connection, table, item_id):
        return self._decode(connection.execute(
            f"SELECT * FROM {table} WHERE id=? AND owner_id=? AND account_id=?",
            (str(item_id), *self._scope())).fetchone())

    def _read(self, table, item_id):
        with closing(self.store._connect()) as db:
            return self._get(db, table, item_id)

    def _save(self, db, table, body, item_id=None, expected_revision=None):
        body = json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))
        if not isinstance(body, dict):
            raise ValueError("Ожидается объект.")
        item_id = item_id or body.get("id") or uuid.uuid4().hex[:24]
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", item_id):
            raise ValueError("Некорректный идентификатор записи.")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision <= 0):
            raise ValueError("Некорректная версия записи.")
        old = self._get(db, table, item_id)
        if old and expected_revision != old["revision"]:
            raise ValueError("Запись изменена; обновите предпросмотр.")
        if not old and expected_revision is not None:
            raise ValueError("Запись не найдена или изменена.")
        for key in ("id", "revision", "created_at", "updated_at"):
            body.pop(key, None)
        game_key = template_id = None
        if table == "product_templates" and body.get("name"):
            game_key = normalize_game(body["name"])
        if table == "product_variants":
            from .product_variants import validate_variant
            template_id = body.get("template_id")
            template = self._get(db, "product_templates", template_id)
            parent = self._get(db, "game_routes", body.get("route_id"))
            body = validate_variant(body, template, parent)
            if old and (old["template_id"], old["route_id"]) != (body["template_id"], body["route_id"]):
                raise ValueError("Область варианта нельзя изменить.")
            game_key = json.dumps([body["route_id"], normalize_game(body["display_name"])], ensure_ascii=False)
        if table == "game_routes":
            allowed = {"template_id", "game_name", "game_aliases", "node_id", "section_name", "metadata", "overrides", "game_key"}
            if set(body) - allowed:
                raise ValueError("Неподдерживаемые поля маршрута.")
            template_id = str(body.get("template_id", ""))
            template = self._get(db, "product_templates", template_id)
            if not template:
                raise ValueError("Шаблон не найден.")
            game_key = normalize_game(body.get("game_name", ""))
            if (not isinstance(body.get("game_name"), str) or not game_key or len(body["game_name"]) > 500
                    or not isinstance(body.get("section_name"), str) or not body["section_name"].strip()
                    or len(body["section_name"]) > 500
                    or type(body.get("node_id")) is not int or not 0 < body["node_id"] <= 2**63 - 1):
                raise ValueError("Некорректный маршрут игры.")
            aliases = body.setdefault("game_aliases", [])
            if (not isinstance(aliases, list) or len(aliases) > 100
                    or any(not isinstance(a, str) or not normalize_game(a) or len(a) > 500 for a in aliases)):
                raise ValueError("Некорректные псевдонимы игры.")
            metadata = body.get("metadata", {})
            overrides = body.get("overrides", {})
            if (not isinstance(metadata, dict) or set(metadata) != {"category_id", "type"}
                    or metadata.get("type") != "COMMON" or type(metadata.get("category_id")) is not int
                    or metadata["category_id"] <= 0):
                raise ValueError("Неподтверждённый раздел игры.")
            if (not isinstance(overrides, dict) or set(overrides) - {
                    "emoji", "title_ru", "title_en", "description_ru", "description_en", "variables"}
                    or any(not isinstance(v, str) for k, v in overrides.items() if k != "variables")
                    or ("variables" in overrides and (not isinstance(overrides["variables"], dict)
                        or set(overrides["variables"]) - set(template.get("allowed_variables", (
                            "game", "platform", "region", "edition", "product_name", "quantity")))
                        or any(not isinstance(k, str) or not isinstance(v, str)
                               for k, v in overrides["variables"].items())))):
                raise ValueError("Неподдерживаемые переопределения маршрута.")
            body["game_key"] = game_key
        now = time.time()
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 1_000_000:
            raise ValueError("Слишком большая запись.")
        try:
            if old:
                cursor = db.execute(f"UPDATE {table} SET body=?,revision=revision+1,updated_at=?,"
                                    "game_key=?,template_id=? WHERE id=? AND owner_id=? AND account_id=? AND revision=?",
                                    (encoded, now, game_key, template_id, item_id, *self._scope(), expected_revision))
                if cursor.rowcount != 1:
                    raise ValueError("Запись изменена; обновите предпросмотр.")
            else:
                db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                           (item_id, *self._scope(), encoded, 1, now, now, game_key, template_id))
        except sqlite3.IntegrityError:
            raise ValueError("Запись уже существует.") from None
        return self._get(db, table, item_id)

    def _write(self, table, body, item_id=None, expected_revision=None):
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._save(db, table, body, item_id, expected_revision)

    def get_template(self, template_id):
        return self._read("product_templates", template_id)

    def save_template(self, body, template_id=None, expected_revision=None):
        return self._write("product_templates", body, template_id, expected_revision)

    def delete_template(self, template_id, expected_revision=None):
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM game_routes WHERE template_id=? AND owner_id=? AND account_id=? LIMIT 1",
                          (template_id, *self._scope())).fetchone():
                raise ValueError("Шаблон используется маршрутами игр.")
            cursor = db.execute("DELETE FROM product_templates WHERE id=? AND owner_id=? AND account_id=? AND revision=?",
                                (template_id, *self._scope(), expected_revision))
            if cursor.rowcount != 1:
                raise ValueError("Шаблон не найден или изменён.")

    def _list(self, table, limit=50, offset=0, template_id=None, query="", recent=False):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            raise ValueError("Некорректная страница списка.")
        with closing(self.store._connect()) as db:
            sql = f"SELECT * FROM {table} WHERE owner_id=? AND account_id=?"
            args = list(self._scope())
            if template_id is not None:
                sql += " AND template_id=?"
                args.append(template_id)
            sql += " ORDER BY " + ("last_used DESC, " if recent else "") + "created_at DESC, id"
            # Stream and filter, keeping only a bounded page in memory (Unicode casefold).
            found = []
            needle = normalize_game(query)
            skip = offset
            for row in db.execute(sql, args):
                item = self._decode(row)
                haystack = [item.get("game_name", ""), *item.get("game_aliases", []), item.get("name", "")]
                if needle and not any(needle in normalize_game(s) for s in haystack):
                    continue
                if skip:
                    skip -= 1
                    continue
                found.append(item)
                if len(found) >= limit:
                    break
            return found

    def list_templates(self, limit=50, offset=0):
        return self._list("product_templates", limit, offset)

    def get_route(self, route_id):
        return self._read("game_routes", route_id)

    def save_route(self, body, expected_revision=None):
        return self._write("game_routes", body, expected_revision=expected_revision)

    def list_routes(self, template_id=None, query="", limit=50, offset=0, recent=False):
        return self._list("game_routes", limit, offset, template_id, query, recent)

    def find_route(self, template_id, game_name):
        key = normalize_game(game_name)
        matches = []
        with closing(self.store._connect()) as db:
            for row in db.execute("SELECT * FROM game_routes WHERE owner_id=? AND account_id=? AND template_id=?", (*self._scope(), template_id)):
                item = self._decode(row)
                if key in [normalize_game(item["game_name"]), *map(normalize_game, item.get("game_aliases", []))]:
                    matches.append(item)
        if len(matches) > 1:
            raise ValueError("Название игры неоднозначно; выберите конкретный маршрут.")
        return matches[0] if matches else None

    def get_variant(self, variant_id):
        return self._read("product_variants", variant_id)

    def save_variant(self, body, variant_id=None, expected_revision=None):
        return self._write("product_variants", body, variant_id, expected_revision)

    def disable_variant(self, variant_id, expected_revision):
        # A status-only action must remain available after template/catalogue edits.
        if type(expected_revision) is not int or expected_revision <= 0:
            raise ValueError("Некорректная версия записи.")
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            body = self._get(db, "product_variants", variant_id)
            if body is None or body["revision"] != expected_revision:
                raise ValueError("Вариант не найден или изменён.")
            for key in ("id", "revision", "created_at", "updated_at"):
                body.pop(key, None)
            body["enabled"] = False
            db.execute("UPDATE product_variants SET body=?,revision=revision+1,updated_at=? "
                       "WHERE id=? AND owner_id=? AND account_id=? AND revision=?",
                       (json.dumps(body, ensure_ascii=False, allow_nan=False), time.time(),
                        variant_id, *self._scope(), expected_revision))
            return self._get(db, "product_variants", variant_id)

    def list_variants(self, route_id, limit=50, offset=0, enabled_only=True):
        if (type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100
                or not 0 <= offset <= 1_000_000 or type(enabled_only) is not bool):
            raise ValueError("Некорректная страница списка.")
        with closing(self.store._connect()) as db:
            found, skip = [], offset
            for row in db.execute("SELECT * FROM product_variants WHERE owner_id=? AND account_id=? "
                                  "ORDER BY created_at DESC,id", self._scope()):
                body = self._decode(row)
                if body["route_id"] != route_id or (enabled_only and not body["enabled"]):
                    continue
                if skip:
                    skip -= 1
                    continue
                found.append(body)
                if len(found) == limit:
                    break
            return found

    def stage_batch(self, kind, bodies):
        if kind not in ("routes", "drafts") or not isinstance(bodies, list) or not 1 <= len(bodies) <= 100:
            raise ValueError("Пакет должен содержать от 1 до 100 строк.")
        token = uuid.uuid4().hex[:24]
        encoded = json.dumps(bodies, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 2_000_000:
            raise ValueError("Слишком большой пакет.")
        with closing(self.store._connect()) as db, db:
            db.execute("INSERT INTO product_batches VALUES (?,?,?,?,?,?,NULL)",
                       (token, *self._scope(), kind, "pending", encoded))
        return self.get_batch(token)

    def _batch(self, db, token):
        row = db.execute("SELECT kind,status,body,result FROM product_batches WHERE token=? AND owner_id=? AND account_id=?", (token, *self._scope())).fetchone()
        if not row:
            raise ValueError("Предпросмотр не найден.")
        return dict(token=token, kind=row[0], status=row[1], rows=json.loads(row[2]),
                    **(json.loads(row[3]) if row[3] else {"results": [], "errors": []}))

    def get_batch(self, token):
        with closing(self.store._connect()) as db:
            return self._batch(db, token)

    def confirm_batch(self, token):
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._batch(db, token)
            if batch["status"] != "pending":
                return batch
            results, errors = [], []
            for index, source in enumerate(batch["rows"]):
                db.execute("SAVEPOINT product_row")
                try:
                    body = dict(source)
                    revision = body.pop("expected_revision", None)
                    row = self._save(db, "game_routes" if batch["kind"] == "routes" else "product_drafts", body, expected_revision=revision)
                    results.append(row)
                    if batch["kind"] == "drafts":
                        route_id = body.get("route_id") or (body.get("route") or {}).get("id")
                        db.execute("UPDATE game_routes SET last_used=? WHERE id=? AND owner_id=? AND account_id=?", (time.time(), route_id, *self._scope()))
                except (ValueError, TypeError):
                    db.execute("ROLLBACK TO product_row")
                    errors.append({"index": index, "message": "Строка не сохранена: запись некорректна, существует или изменена."})
                finally:
                    db.execute("RELEASE product_row")
            result = dict(results=results, errors=errors)
            db.execute("UPDATE product_batches SET status='confirmed',result=? WHERE token=?", (json.dumps(result, ensure_ascii=False), token))
            return dict(batch, status="confirmed", **result)

    def cancel_batch(self, token):
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._batch(db, token)
            db.execute("UPDATE product_batches SET status='cancelled' WHERE token=? AND status='pending'", (token,))
            return self._batch(db, token)

    def stage_draft_batch(self, bodies):
        return self.stage_batch("drafts", bodies)["token"]

    def confirm_draft_batch(self, token):
        return self.confirm_batch(token)["results"]

    def cancel_draft_batch(self, token):
        return self.cancel_batch(token)

    def get_draft(self, draft_id):
        return self._read("product_drafts", draft_id)

    def list_drafts(self, limit=50, offset=0):
        return self._list("product_drafts", limit, offset)

    def edit_draft(self, draft_id, body, expected_revision):
        return self._write("product_drafts", body, draft_id, expected_revision)

    def cancel_draft(self, draft_id, expected_revision):
        body = self.get_draft(draft_id)
        if body is None:
            raise ValueError("Черновик не найден.")
        body["status"] = "cancelled"
        return self.edit_draft(draft_id, body, expected_revision)
