"""Offline QA runner for product user journeys and core regression contracts.

Never starts the bot. Selected tests use mocked Telegram/FunPay and temporary data.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


PRODUCT_TEST_FILES = (
    "tests/test_qa_user_journeys.py",
    "tests/test_node_form.py",
    "tests/test_node_form_diagnostics.py",
    "tests/test_node_products.py",
    "tests/test_telegram_products.py",
    "tests/test_product_pickers.py",
    "tests/test_similar_drafts.py",
    "tests/test_game_catalog.py",
    "tests/test_realtime_routes.py",
    "tests/test_product_compatibility.py",
)


AREAS = {
    "test_qa_user_journeys.py": "User journeys",
    "test_node_form.py": "Node schema / GET only",
    "test_node_form_diagnostics.py": "Safe diagnostics",
    "test_node_products.py": "Node fields / drafts / clone",
    "test_telegram_products.py": "Telegram product interface",
    "test_product_pickers.py": "Variable buttons / quantity",
    "test_similar_drafts.py": "Create Similar",
    "test_game_catalog.py": "Game catalog",
    "test_realtime_routes.py": "GameRoute resolver",
    "test_product_compatibility.py": "Legacy compatibility",
}


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ")[:180]


def report(path: Path, suite: str, exit_code: int) -> tuple[str, bool]:
    results: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0, "skip": 0})
    failing_ids: list[str] = []
    count = 0
    parse_error = False
    try:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            count += 1
            filename = Path(case.get("file") or case.get("classname", "unknown").replace(".", "/")).name
            area = AREAS.get(filename, filename)
            bucket = results[area]
            if case.find("failure") is not None or case.find("error") is not None:
                bucket["fail"] += 1
                if len(failing_ids) < 15:
                    failing_ids.append(_escape_cell(f"{filename}::{case.get('name', 'unnamed')}"))
            elif case.find("skipped") is not None:
                bucket["skip"] += 1
            else:
                bucket["pass"] += 1
    except (OSError, ET.ParseError):
        parse_error = True

    passes = sum(x["pass"] for x in results.values())
    failures = sum(x["fail"] for x in results.values())
    skipped = sum(x["skip"] for x in results.values())
    ok = exit_code == 0 and count > 0 and failures == 0 and not parse_error
    lines = [
        "# FunPayFlow automated QA",
        "",
        f"**Outcome: {'PASS' if ok else 'FAIL'}** — {suite} offline suite.",
        "",
        "This checks mocked Telegram handlers, FunPay form fixtures, and temporary SQLite. "
        "It does **not** validate current live FunPay HTML, publish lots, or contact production.",
        "",
        f"**Results:** {passes} passed · {failures} failed · {skipped} skipped. "
        f"Pytest exit code: {exit_code}.",
        "",
        "| Area | Passed | Failed | Skipped |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, stats in sorted(results.items()):
        lines.append(f"| {_escape_cell(name)} | {stats['pass']} | {stats['fail']} | {stats['skip']} |")
    if failing_ids:
        lines.extend(["", "## Failed test identifiers", ""])
        lines.extend(f"- `{identifier.replace('`', '')}`" for identifier in failing_ids)
    if parse_error or count == 0:
        lines.extend(["", "No readable JUnit test results; check the GitHub Actions job logs."])
    if exit_code != 0 and failures == 0:
        lines.extend(["", "Pytest failed outside reported test assertions; inspect job logs."])
    lines.extend(["", "**Security boundary:** No live FunPay or Telegram credentials are loaded by this QA suite.", ""])
    return "\n".join(lines), ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline FunPayFlow QA with concise Markdown results")
    parser.add_argument("--suite", choices=("product", "full"), default="product")
    parser.add_argument("--report-dir", type=Path, default=Path(".qa-reports"))
    opts = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    report_dir = (repo / opts.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    junit = report_dir / "results.xml"
    selected = PRODUCT_TEST_FILES if opts.suite == "product" else ("tests",)
    missing = [item for item in selected if not (repo / item).exists()]
    if missing:
        summary = "# FunPayFlow automated QA\n\n**FAIL** — required test files are missing: " + ", ".join(missing) + "\n"
        (report_dir / "summary.md").write_text(summary, encoding="utf-8")
        print(summary)
        return 2

    command = [sys.executable, "-m", "pytest", "-q", *selected, f"--junitxml={junit}"]
    print("Running offline pytest QA suite:", opts.suite, flush=True)
    try:
        code = subprocess.run(command, cwd=repo, check=False, timeout=600).returncode
    except subprocess.TimeoutExpired:
        code = 124
        print("QA pytest exceeded the 600-second safety timeout.", file=sys.stderr)
    summary, ok = report(junit, opts.suite, code)
    (report_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    return 0 if ok else (code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
