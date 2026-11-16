---
title: RAG API
sidebar_position: 1
---

RAG API работает в контейнере `core`: принимает вопросы Mini App по HTTP, получает Telegram updates через внутренний polling intake и доставляет результаты. Сам бот только приглашает открыть Mini App. Внутри него находятся **Access** — допуск сотрудников — и **Support** — обращения, тикеты и админ-панель. Поиск, проверки вопроса, OCR и генерацию оркестрирует RAG Core; связь с ним идёт через RabbitMQ.

- [Устройство модуля](./module/index.md) — границы, входы и данные.
- [Mini App: HTTP, роли и запуск](./mini-app.md), [новые DTO](./mini-app-contracts.md) — текстовый веб-канал и совместимость с Telegram.
- [Приём и доставка](./module/queue-processing.md) — очередь, повторы и ограничения.
- [Доступ сотрудников](./module/access.md) — username для допуска, User ID для владельца и одного активного вопроса.
- [Поддержка и тикеты](./module/support.md) — кнопки, автоматическая эскалация и ожидание.
- [Точки входа](./module/entrypoints.md), [DTO](./module/contracts.md) и [HTTP-ручки](./endpoints/index.md) — целевые контракты; [функции компонентов](./module/service-entrypoints.md).

[Python-функции текущего HTTP-адаптера](./module/http-adapter-entrypoints.md).

В серверном развёртывании обновления поступают только через [long polling](../infrastructure/server-deployment.md#long-polling-вместо-входящих-webhook-запросов): отдельный poller передаёт их существующему HTTP-адаптеру во внутренней сети. Внешний webhook удалён; контракты /api/v1/* Mini App сохраняются.
