---
title: Runtime
sidebar_position: 1
---

## Граница

Runtime конфигурирует [Model Gateway](../model-gateway/index.md) и управляет его запуском; технические обращения к моделям принадлежат Model Gateway. Runtime собирает модули в контейнерах `core` и `worker`, даёт им доступ к хранилищам и запускает восстановление прерванных этапов. Состояние меняют владельцы этапов. **[Queue Relay](./queue-relay.md)** публикует записи outbox в RabbitMQ. [Сборка модулей](../architecture/module-composition.md) · [Состояния и сбои](../architecture/lifecycle.md)

Вызовы к адаптерам ниже — асинхронные функции Python. В разделах указано, кто вызывает их и какие DTO возвращаются. Параметры подключения передаёт Runtime при сборке. [Значения тегов](../faq/index.md#теги-контрактов)

## Компоненты

| Компонент | Роль |
| --- | --- |
| [Запуск Runtime](./startup.md) | Сервисы, тома, роли процессов и порядок запуска. |
| [PostgreSQL](./postgresql/index.md) | Девять таблиц: состояние RAG/редакций/outbox, доступ, доставки и поддержка. [Функции](./postgresql/index.md#что-вызывать) для RAG API, RAG Core, Ingestion и Queue Relay. |
| [RabbitMQ](./rabbitmq/index.md) | Три очереди, формат уведомлений и [функции публикации и обработки](./rabbitmq/contracts.md). |
| [Qdrant](./qdrant/index.md) | Отдельные наборы чанков и примеров темы. [Функции поиска, записи и удаления точек](./qdrant/contracts.md). |
| [Redis](./redis/index.md) | Кеш готовых ответов и [функции адаптера Exact Cache](./redis/contracts.md). |
| [S3 · изображения](./s3.md) | Запись изображений Ingestion через `put_image`, чтение для доставки RAG API через `get_image`. |
| [Telegram-медиа и источники](./media.md) | Скачивание входящего изображения для OCR и метаданные HTML-заглушки. |
| [Временные файлы загрузки](./upload-spool/index.md) | Рабочий том готового Markdown и PreparedRevision; [функции записи, чтения и очистки](./upload-spool/contracts.md). |

request_id связывает вопрос и бизнес-итог, delivery_id — самостоятельную доставку; одна регистрация может породить несколько уведомлений. Идентификатор задания связывает загрузку, редакцию и индексацию. PostgreSQL хранит их состояние, RabbitMQ переносит уведомления. [Владение данными и версии](../architecture/data-and-versions.md)
