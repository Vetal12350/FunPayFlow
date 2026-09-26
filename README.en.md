[Русский](README.md) | English

<div align="center">

# FunPayFlow

**Automation, sales analytics, and Telegram controls for FunPay sellers**

An open-source toolkit that helps sellers manage routine work and understand their sales.

[![Release](https://img.shields.io/github/v/release/Vetal12350/FunPayFlow?label=release)](https://github.com/Vetal12350/FunPayFlow/releases/latest)
[![CI](https://github.com/Vetal12350/FunPayFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/Vetal12350/FunPayFlow/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Quick start](#quick-start) · [Features](#features) · [Windows or VPS](#windows-or-vps) · [FAQ](#faq) · [Русский](README.md)

</div>

FunPayFlow is an open-source bot for FunPay sellers. It combines autobump, order notifications, persistent sales history, statistics, and sales analytics in a Telegram control panel. Run it on Windows or Linux. A VPS is optional; it is useful when the bot should stay online after your home computer is turned off.

FunPayFlow is an independent open-source project and is not affiliated with
or endorsed by FunPay. **The Telegram interface is Russian-only in v1.0.** The Windows installer offers RU/EN for setup and startup messages, not for Telegram.

## Quick start

**Windows:** open the [latest FunPayFlow release](https://github.com/Vetal12350/FunPayFlow/releases/latest), download the **`FunPayFlow-vX.Y.Z.zip`** asset, and extract it. GitHub's automatically generated “Source code (zip)” is not the beginner installer.

1. Run `Setup.bat` from the extracted folder. It prepares Python 3.13 and dependencies; a manual Python installation is normally unnecessary.
2. Enter the required FunPay and Telegram configuration in hidden input fields.
3. Run `Start.bat`, keep the window open, and send `/start` to your bot.

Program files stay in the extracted folder. Private settings and SQLite data stay separately in `%LOCALAPPDATA%\FunPayFlow`. Setup preserves existing configuration when you choose to keep it. If `winget` is unavailable, you may need to [install uv](https://docs.astral.sh/uv/getting-started/installation/) manually.

For 24/7 use independent of your PC, follow the [step-by-step VPS guide (Russian)](docs/VPS_INSTALL.md).

> Automation can modify your FunPay account. Review auto-reply text and the platform rules. `SAFE_MODE` blocks automatic modifying actions.

## Features

| Area | Capabilities |
| --- | --- |
| Automation | Lot autobump with cooldown tracking and bounded retries; night auto-reply; post-order review requests. |
| Sales | Order, message, and review notifications; persistent order history; SQLite-backed statistics and sales analytics. |
| Data | Preview and deduplicated import of the official FunPay ZIP export; currency-specific totals without conversion or mixed-currency sums. |
| Telegram | Ten built-in modules, status, issues, action journal, logs, and graceful restart. |
| Reliability | Process lock, persisted events and settings, `SAFE_MODE`, and a final action gate before modifying operations. |

The modules are `autobump`, `notifications`, `night_mode`, `review_request`, `order_history`, `statistics`, `sales_analytics`, `sales_import`, `withdrawals`, and `logs_ui`. All ten are preselected on a fresh installation; module changes apply after restart. Night replies and review requests also have their own switches. Each order retains its historical currency; analytics never adds USD, RUB, and other currencies together.

## Why FunPayFlow

The Windows installer simplifies first setup. Telegram provides day-to-day controls. A separate private data directory makes release-folder updates easier. Linux and an optional systemd service support VPS use. The source is public and built-in functions can be enabled separately.

## Windows or VPS

| Location | Best fit | Start here |
| --- | --- | --- |
| Windows 10/11 | Easy first run while the PC remains on | Release ZIP → `Setup.bat` → `Start.bat` |
| Ubuntu VPS | Operation independent of the home PC | [Beginner VPS guide (Russian)](docs/VPS_INSTALL.md) |

The Linux `linux/install.sh` script installs from `uv.lock` and can set up `funpayflow.service`. Run the installer as a regular user; `sudo` is used for the optional service installation.

## Private data and updates

Windows Setup/Start use `%LOCALAPPDATA%\FunPayFlow` (falling back to `%USERPROFILE%\AppData\Local\FunPayFlow`). The Linux installer suggests `~/.local/share/funpayflow`. Advanced users may set an absolute `FUNPAY_BOT_DATA_DIR`. A manual run without that variable retains the legacy code-local layout; legacy data is not migrated automatically.

Before updating, stop the bot and back up the **entire private data directory**. On Windows, extract the new release to a new folder, run its `Setup.bat` while keeping the existing configuration, then run its `Start.bat`. No manual copy of SQLite/settings/logs is needed in the normal Windows flow. On Linux, follow the [update steps (Russian)](docs/VPS_INSTALL.md#12-обновление).

## FAQ

**Do I need a VPS?** No. Windows works while your PC and `Start.bat` remain running. A VPS is useful for 24/7 operation independent of your PC.

**Do I need to install Python myself?** The installers prepare Python 3.13 using `uv`. If `winget` or `uv` is unavailable, install `uv` manually.

**Where are my settings?** In the private data directory, separate from program code for normal Windows/VPS installation.

**How do I update?** Stop the bot, back up the data directory, then use a new release with the same data location. See the [VPS guide (Russian)](docs/VPS_INSTALL.md#12-обновление).

**The bot does not respond. What should I check?** Confirm setup is complete, only one instance runs, local configuration is correct, and network access works. See [troubleshooting (Russian)](docs/VPS_INSTALL.md#14-частые-ошибки).

**Where do I report a problem?** Use [GitHub Issues](https://github.com/Vetal12350/FunPayFlow/issues) for bugs and feature requests. Report vulnerabilities privately through [GitHub Private Vulnerability Reporting](https://github.com/Vetal12350/FunPayFlow/security/advisories/new) as described in [SECURITY.md](SECURITY.md). Never include secrets or private logs in public issues.

## Development

In a source checkout, install `uv` and Python 3.13+, then run:

```text
uv sync --locked --dev
uv run --no-sync pytest -q
uv run --no-sync python tests/packaging_smoke.py
```

For manual configuration, run `uv run --no-sync python -m funpayflow.setup_config --data-dir <absolute_path>`. The supported application entry points are `uv run --no-sync funpayflow` and `uv run --no-sync python -m funpayflow.main`. The commands above that check source/tests do not start a FunPay or Telegram session.

## Security and license

Never publish `.env`, `bot_settings.json`, SQLite databases, sales exports, logs, or backups. Secrets are entered locally, not through Telegram. Read the [security policy](SECURITY.md).

FunPayFlow is released under the MIT License. See LICENSE.
