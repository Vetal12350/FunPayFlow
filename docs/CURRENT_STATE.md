# Текущее состояние: snapshot, не changelog

Обновлено: **2026-10-16**. Перед новой задачей перепроверь Git; документ не заменяет текущий HEAD/status.

- Исследованный source baseline: `main`, `76033af34a154bd5fab68c73eb9a9220d3cf20a0` — `fix: persist autobump recovery and reduce runtime notification noise`.
- Ветка подготовки этой документации: `codex/project-context`, base commit указан выше. Текущий commit документации и состояние worktree определять через Git, не по этому snapshot.
- Stable release: `v1.0.0` (публикация зафиксирована владельцем и RELEASE_CHECKLIST; локальный tag указывает на `b288ef2`). Package metadata по-прежнему `1.0.0`. HEAD содержит post-release изменения: это не доказательство наличия нового кода в опубликованном ZIP. Tag/assets v1.0.0 не пересоздавать.
- Реализовано: src-layout package/install flow; Telegram modules/setup, orders/statistics/multi-currency analytics и official sales import; review persistence; transient polling TLS/network recovery; persistent autobump claim/progress/cooldown и resume; сохранение ручного OFF; подавление runtime Telegram noise и persistent critical-category dedup.
- Пакет документации: `AGENTS.md` и шесть выборочных документов в `docs/`. Активная feature-разработка по tracked baseline не установлена; локальные untracked каталоги не считались частью приложения.
- **PLANNED:** создание новых FunPay-лотов по собственным существующим лотам-шаблонам; настраиваемое расписание, управляющее только autobump. В текущих `src/` нет реализации этих flows/модулей. Существующее scheduling review requests и systemd template не являются ими.
- Важные границы: UNKNOWN не доказывает failure/success; после crash возможен консервативный пропуск поднятия на safe window. Время cooldown зависит от wall clock. Active polling recovery не равен startup retry. Сохранённый legacy OFF не имеет provenance — нельзя автоматически угадать намерение пользователя.
- Live VPS state, deployed commit, backups и runtime health **не проверены**. Owner snapshot миграции от 2026-10-13 вынесен в [PRODUCTION](PRODUCTION.md); не считать его текущим фактом.

Подробности поведения: [FUNPAY_BEHAVIOR](FUNPAY_BEHAVIOR.md). Обновлять этот файл после значимого завершённого пакета, если изменились capabilities/границы/WIP; не добавлять строку для каждой мелкой правки или выдуманные результаты тестов.
