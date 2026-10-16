import unittest

from funpayflow.product_templates import ProductTemplate, ProductValidationError, default_template


class ProductTemplateTests(unittest.TestCase):
    def test_builtin_bilingual_defaults_and_explicit_overrides(self):
        rendered = ProductTemplate(default_template()).render({"game": "Game", "product_name": "Key"})
        self.assertIn("Steam | Global | Standard", rendered["title_ru"])
        self.assertIn("Quantity: 1", rendered["description_en"])
        self.assertIn("Количество: 1", rendered["description_ru"])
        changed = ProductTemplate(default_template()).render(
            {"game": "Game", "product_name": "Key"}, {"emoji": "⭐", "variables": {"region": "EU"}})
        self.assertEqual(changed["emoji"], "⭐")
        self.assertIn("EU", changed["title_en"])

    def test_unknown_and_missing_variables_and_unsafe_syntax_fail(self):
        model = ProductTemplate(default_template())
        for values in ({}, {"game": "Game", "product_name": "X", "secret": "hidden"},
                       {"game": "{{price}}", "product_name": "Key"},
                       {"game": "G", "product_name": "P", "quantity": True}):
            with self.subTest(values=values), self.assertRaises(ProductValidationError):
                model.render(values)
        for syntax in ("{{game.name}}", "{{game[0]}}", "{{game!r}}", "{{{{game}}}}", "{game}"):
            body = default_template()
            body["title_ru"] = syntax
            with self.subTest(syntax=syntax), self.assertRaises(ProductValidationError):
                ProductTemplate(body)

    def test_required_bilingual_text_unknown_fields_and_validation_rules(self):
        for field in ("title_ru", "title_en", "description_ru", "description_en"):
            body = default_template()
            body[field] = ""
            with self.assertRaises(ProductValidationError):
                ProductTemplate(body)
        body = default_template()
        body["validation_rules"] = {"region": {"choices": ["EU"], "max_length": 5}}
        with self.assertRaises(ProductValidationError):
            ProductTemplate(body).render({"game": "Game", "product_name": "Key"})
        with self.assertRaises(ProductValidationError):
            ProductTemplate(default_template()).render({"game": "Game", "product_name": "Key"}, {"node_id": 1})

    def test_title_emoji_is_rendered_once_and_final_length_is_bounded(self):
        body = default_template()
        body["title_ru"] = "🔑 {{game}}"
        rendered = ProductTemplate(body).render({"game": "Game", "product_name": "Key"})
        self.assertEqual(rendered["title_ru"], "🔑 Game")
        body["title_ru"] = "x" * 200
        with self.assertRaises(ProductValidationError):
            ProductTemplate(body).render({"game": "Game", "product_name": "Key"})

    def test_credential_and_service_variables_cannot_be_requested(self):
        names = ("golden_key", "csrf", "csrf_token", "form_created_at", "phpsessid", "cookie",
                 "cookies", "password", "passwords", "bot_token", "api_key", "token", "offer_id",
                 "node_id", "publish_state", "delivery_secrets", "price", "amount", "account_id",
                 "access_token", "refresh_token", "session_id", "api_secret", "secret_key", "authorization")
        for name in names:
            body = default_template()
            body["allowed_variables"].append(name)
            with self.subTest(name=name), self.assertRaises(ProductValidationError):
                ProductTemplate(body)
        body = default_template()
        body["allowed_variables"].append("delivery_note")
        body["description_en"] = "{{delivery_note}}"
        self.assertEqual(ProductTemplate(body).render({"game": "G", "product_name": "P", "delivery_note": "Public note"})["description_en"], "Public note")


if __name__ == "__main__":
    unittest.main()
