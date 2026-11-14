---
title: RAG API
sidebar_position: 1
---

RAG API работает в контейнере `core`: принимает Telegram webhook, регистрирует вопросы и доставляет результаты. Внутри него находятся **Access** — допуск сотрудников — и **Support** — обращения, тикеты и админ-панель. Поиск, проверки вопроса, OCR и генерацию оркестрирует RAG Core; связь с ним идёт через RabbitMQ.

- [Устройство модуля](./module/index.md) — границы, входы и данные.
- [Приём и доставка](./module/queue-processing.md) — очередь, повторы и ограничения.
- [Доступ сотрудников](./module/access.md) — User ID, допуск и один активный вопрос.
- [Поддержка и тикеты](./module/support.md) — кнопки, автоматическая эскалация и ожидание.
- [Точки входа](./module/entrypoints.md), [DTO](./module/contracts.md) и [HTTP-ручки](./endpoints/index.md) — целевые контракты; [функции компонентов](./module/service-entrypoints.md).
