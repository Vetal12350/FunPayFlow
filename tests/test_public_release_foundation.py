"""Offline checks for the public-release foundation."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
from dotenv import dotenv_values

import funpay


ROOT = Path(__file__).resolve().parents[1]


class CookieHostTests(unittest.TestCase):
    def test_exact_https_host_only(self):
        accepted = (
            "https://funpay.com",
            "https://funpay.com/runner/",
            "https://FUNPAY.COM:443/lots/raise",
        )
        rejected = (
            "http://funpay.com/runner/",
            "https://api.funpay.com/runner/",  # Not used by FunPayAPI 1.1.0.
            "https://evilfunpay.com/",
            "https://funpay.com.evil.example/",
            "https://example.com/?next=funpay.com",
            "https://funpay.com@evil.example/",
            "https://user@funpay.com/",
            "https://funpay.com:444/",
            "https://funpay.com:invalid/",
            "https://[broken/",
            "funpay.com/runner/",
            None,
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(funpay._is_funpay_request_url(url))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(funpay._is_funpay_request_url(url))

    def test_patch_sends_and_records_extra_cookies_only_for_exact_host(self):
        sent = []

        def fake_request(_session, _method, url, *args, **kwargs):
            sent.append((url, dict(kwargs.get("headers") or {})))
            return SimpleNamespace(cookies=SimpleNamespace(
                get_dict=lambda: ({"legitimate_marker": "fixture"}
                                  if url == "https://funpay.com/runner/" else
                                  {"lookalike_marker": "fixture"})))

        with (patch.object(requests.sessions.Session, "request", fake_request),
              patch.object(funpay, "_funpay_cookie_patch_applied", False),
              patch.dict(funpay._funpay_extra_cookies,
                         {"synthetic_marker": "fixture"}, clear=True)):
            funpay._apply_funpay_cookie_patch()
            session = requests.sessions.Session()
            for url in ("https://funpay.com/runner/", "https://evilfunpay.com/",
                        "https://funpay.com.evil.example/",
                        "https://example.com/?next=funpay.com"):
                session.request("GET", url, headers={})
            self.assertIn("synthetic_marker=fixture", sent[0][1].get("cookie", ""))
            for _, headers in sent[1:]:
                self.assertNotIn("cookie", headers)
            self.assertIn("legitimate_marker", funpay._funpay_extra_cookies)
            self.assertNotIn("lookalike_marker", funpay._funpay_extra_cookies)


class ReleaseConfigurationTests(unittest.TestCase):
    def test_example_matches_required_and_optional_runtime_names(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        keys = {line.split("=", 1)[0] for line in lines
                if line and not line.startswith("#")}
        self.assertEqual(keys, {"FUNPAY_GOLDEN_KEY", "BOT_TOKEN", "ADMIN_ID",
                                "FUNPAY_USER_ID", "BOT_PASSWORD", "DEBUG"})
        parsed = dotenv_values(ROOT / ".env.example")
        self.assertEqual(set(parsed), keys)
        self.assertEqual(parsed["BOT_PASSWORD"], "")
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        required = {node.args[0].value for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_require_env" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)}
        self.assertEqual(required, {"FUNPAY_GOLDEN_KEY", "BOT_TOKEN",
                                    "ADMIN_ID", "FUNPAY_USER_ID"})

    def test_obsolete_patch_scripts_are_not_runtime_dependencies(self):
        for name in ("cleanup", "revert"):
            self.assertFalse((ROOT / f"{name}.py").exists())
        for path in ROOT.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name in {"cleanup", "revert"}
                                         for alias in node.names))
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"cleanup", "revert"})


if __name__ == "__main__":
    unittest.main()
