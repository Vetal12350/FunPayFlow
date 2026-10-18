# Локальный Codex Worker

Worker связывает задачи из ChatGPT и GitHub Issues с локальным Codex CLI:
он готовит изменения в отдельном source checkout и создаёт Pull Request (PR).
Работа выполняется на включённом Windows-компьютере владельца.

## Разовая подготовка

Нужны Git, GitHub CLI (`gh`), Codex CLI, `uv` и Python 3.13.
Исходники worker должны находиться в `%LOCALAPPDATA%\FunPayFlowCodexWorker\source`.
В терминале выполните вход в GitHub и Codex:

```text
gh auth login
codex.cmd login
```

Для `gh` используйте GitHub-аккаунт владельца репозитория с правами на PR.
В Codex выберите вход через ChatGPT и авторизуйтесь в браузере
с аккаунтом ChatGPT Plus. Секреты в issue или документацию не вставляйте.

## Проверка и ручной запуск

В **cmd.exe** проверьте доступность инструментов и авторизацию:

```bat
py -3.13 "%LOCALAPPDATA%\FunPayFlowCodexWorker\source\scripts\codex_issue_worker.py" --check
```

`--check` проверяет инструменты и статус входа, но не обрабатывает задачи.
Для ручного запуска выполните в **PowerShell**:

```powershell
& "$env:LOCALAPPDATA\FunPayFlowCodexWorker\source\scripts\start_codex_worker.ps1"
```

Launcher обновляет свой source checkout через `git pull --ff-only`
и запускает опрос очереди каждые 90 секунд. Для остановки нажмите `Ctrl+C`.

## Очередь задач и результат

- Очередь — открытые GitHub issues в `Vetal12350/FunPayFlow` с меткой `codex-local`.
- Принимаются задачи автора `Vetal12350` с заголовком, начинающимся
  с `[Codex] `, и непустым описанием; одной метки недостаточно.
- Задачи выполняются последовательно, по возрастанию номера, каждая
  в отдельном checkout и ветке `codex/issue-<номер>`.
- Локальное состояние предотвращает повторную обработку уже взятых задач,
  включая заблокированные: после ошибки требуется разбор на локальном ПК.
- Доверенный worker публикует ветку и PR, оставляет ссылку в issue и закрывает её.

Изменения проходят через PR, проверку и существующий CI с офлайн-тестами.
Закрытая issue означает создание PR, а не выпуск изменений.
Автоматического merge или попадания в production нет.

Worker работает только с исходниками: рабочий бот FunPay, VPS и реальные лоты
не затрагиваются. Он не запускает FunPayFlow и не обращается к реальным
FunPay/Telegram; публикация лотов и deployment в этот процесс не входят.
