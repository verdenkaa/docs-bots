---
title: FAQ команды
sidebar_position: 1
---

:::warning Рабочий справочник
Обновим после получения полного кейса и первого сквозного запуска.
:::

## Где смотреть актуальные решения?

Начните с [архитектуры MVP](../architecture/index.md). Первоисточник решений команды — столбцы «Итог» в [командном синке](./drafts/architecture-team-sync.md). Ранние исследования собраны в подразделе «Черновики» этого FAQ.

## Что означают термины и обозначения?

[Глоссарий](./glossary.md) собирает имена модулей, понятия корпуса и редакций, идентификаторы, DTO, состояния и инфраструктурные термины. Там же есть соответствия прежних названий из черновиков текущей модели MVP.

## Что проверить, когда выдадут кейс?

Заполнить таблицу открытых решений по заданию и среде запуска. Для проверки готовой реализации использовать отдельный список сценариев. [Чек-лист кейса](./case-checklist.md) · [Проверки первого среза](./mvp-checklist.md) · [Материалы по кейсу](./drafts/case.md)

## Как запустить стенд?

В папке `container-manager/` есть сценарии для баз и очередей, приложения и полного стенда. [Команды запуска и диагностики](./container-manager.md)

## Как проследить запрос в логах?

[Логирование запросов](./logging.md) описывает формат записи, замеры шагов и поиск по `request_id`.

## Где искать идеи для реализации?

[Оптимизации и фичи после среза](./optimization-and-features.md) связывают каждую идею с ситуацией, в которой её стоит попробовать, и результатом, который нужно проверить.

| Материал | Что смотреть |
| --- | --- |
| [Candidate v1](./drafts/candidate_architecture_v1_rag_bot.md) | Стыки модулей, обработка документов, варианты публикации, доставка. |
| [Корпоративное приложение](./drafts/candidate_architecture_v1_appendix_corporate_context_scope.md) | Идентификация, доступ и ограничения передачи данных, если понадобятся по кейсу. |
| [Обзор исследований](./drafts/rag-research-overview.md) · [№1](./drafts/rag-research-2026-10-27.md) · [№2](./drafts/rag-research-2026-11-01.md) | Альтернативы поиска, версионирования и кеша, способы измерить качество и задержку. |
| [Смежные репозитории](./drafts/related-repositories-review.md) | Локаторы источников, статусы индексации, проверка полноты поиска и объединение одинаковых запросов. |
| [Python и Rust](./drafts/rust_decision_report.md) | Синтетический бенчмарк и план профилирования. Прирост на реальном корпусе нужно измерить. |

## Как дописывать документацию?

Начинайте с владельца операции и смысла входа и результата. Добавляйте поля DTO и сигнатуры после согласования стыка. Для HTTP используйте шаблон из `docs/templates/http-endpoint.md`. Правила оформления и команда сборки есть в `docs/AGENTS.md` и `docs/README.md`.

## Теги контрактов

| Тег | Цвет | Что обозначает |
| --- | --- | --- |
| <span className="operation-badge operation-badge--async">ASYNC</span> | `#EC4899` | Внутренний вызов Python с `await`. |
| <span className="operation-badge operation-badge--call">CALL</span> | `#EAB308` | Синхронный вызов Python. |
| <span className="operation-badge operation-badge--get">GET</span> | `#10B981` | HTTP-ручка чтения или проверки состояния. |
| <span className="operation-badge operation-badge--post">POST</span> <span className="operation-badge operation-badge--put">PUT</span> | `#EF4444` | HTTP-ручка для приёма данных, записи или запуска операции. |
| <span className="operation-badge operation-badge--task">TASK</span> <span className="operation-badge operation-badge--sub">SUB</span> | `#8B5CF6` | Фоновая работа с RabbitMQ и обработчик входящего сообщения соответственно. |
| <span className="operation-badge operation-badge--cache">CACHE</span> | `#F43F5E` | Вызов Exact Cache или его адаптера Redis. |
| <span className="operation-badge operation-badge--vec">VEC</span> <span className="operation-badge operation-badge--db">DB</span> | `#06B6D4` | Векторный поиск в Qdrant и операции хранилища соответственно. |

Для Python-вызова способ вызова указан в сигнатуре. Например, `async def` у функции с тегом `DB` означает, что её нужно вызывать с `await`.

## Как работать с DTO в коде?

[DTO и Pydantic](./dto.md): создание объектов, проверка входных данных, сериализация JSON, потоки файлов и проверки контрактов.
