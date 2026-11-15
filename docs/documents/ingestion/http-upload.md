---
title: HTTP-загрузка документа
sidebar_position: 3
---

:::warning Готовность зависимостей
HTTP-адаптер и Gateway используют существующие Python-контракты. Конвертер PDF/DOCX, разбиение и расширения Runtime для DocumentBatch разрабатываются отдельно. Пока зависимость остаётся заглушкой, приём возвращает 503. Сквозная готовность индексации этим не подтверждается.
:::

## <span className="operation-badge operation-badge--post">POST</span> `/admin/ingestion/upload`

Административный вход Ingestion в контейнере worker. Подключение к тикет-системе планируется отдельно; ручки чтения статуса нет.

## Доступ

Обе проверки — до чтения тела и вызова Gateway: настроен непустой `RAG_INGESTION_TOKEN` и заголовок `Authorization: Bearer <token>` совпадает с ним. Токен загружается из окружения или единого `.env` в корне репозитория (либо файла `RAG_ENV_FILE`); окружение имеет приоритет. Сравнение выполняется без зависимости времени от совпавшего префикса. При пустом токене приложение не запускается. Токен не записывается в ответы и логи. Внешний доступ требует HTTPS на входном прокси.

## Что принимает

`Content-Type: application/octet-stream`, `application/pdf` либо `application/vnd.openxmlformats-officedocument.wordprocessingml.document`. Тело — исходные байты одного файла, без multipart, JSON и base64. Оригинал не сохраняется на диск HTTP-адаптером. Тип файла проверяет конвертер; расширение само по себе не подтверждает формат.

| Query-параметр | Тип | Обязательность и смысл |
| --- | --- | --- |
| filename | непустая строка | Обязателен. Точное имя документа, включая `.pdf` или `.docx`; не путь. |
| access_level | непустая строка | Обязателен. Метка доступа документа от доверенного администратора; для общего корпуса — `employees`. Не включает фильтрацию поиска. |
| document_id | UUID | Необязателен. ID обновляемого документа согласно контракту register_revision. |

`RAG_INGESTION_MAX_UPLOAD_BYTES` — предел исходных байтов, по умолчанию 20971520 (20 МиБ). Проверяются Content-Length и фактически прочитанные порции, в том числе без Content-Length. `RAG_INGESTION_UPLOAD_TIMEOUT_S` — общий срок приёма, по умолчанию 600 секунд. Конвертер обязан останавливать управляемого исполнителя и потомков при отмене; Gateway не заменяет это требование одной отменой корутины.

```bash
curl --request POST 'http://127.0.0.1:8001/admin/ingestion/upload?filename=manual.pdf&access_level=employees' \
  --header "Authorization: Bearer $RAG_INGESTION_TOKEN" \
  --header 'Content-Type: application/pdf' \
  --data-binary @manual.pdf
```

## Бизнес-логика

После допуска и проверки метаданных адаптер передаёт поток в [accept_upload](./entrypoints.md#accept-upload). Gateway выполняет конвертацию, запись изображений, Markdown, разбиение и устойчивое сохранение DocumentBatch. Только подтверждённая регистрация редакции и outbox означает accepted. HTTP не публикует сообщение в RabbitMQ напрямую и не выполняет индексацию.

## Что отдаёт

HTTP 202, JSON существующего AcceptedUpload:

```json
{"kind":"accepted","document_id":"43f95f66-7343-4cbc-beb5-19fd36a8ca77","job_id":"bb32ecbf-082b-4497-a41f-4b61aa56a139"}
```

Это надёжный приём задания, а не готовый поисковый индекс. Ответы используют `Cache-Control: no-store`.

## Ошибки и повторы

| HTTP | Тело / detail | Условие |
| --- | --- | --- |
| 400 | UploadRejected: invalid_upload | Пустое тело или неправильный Content-Length. |
| 401 | detail: unauthorized | Отсутствующий, неправильный токен или схема; `WWW-Authenticate: Bearer`. |
| 404 | UploadRejected: unknown_document | PostgreSQL подтвердил неизвестный document_id; временная загрузка удаляется. |
| 413 | UploadRejected: too_large | Превышен размер исходного файла. |
| 415 | UploadRejected: unsupported_format | Неподдерживаемый Content-Type, расширение или отказ конвертера по формату. |
| 422 | detail: invalid_upload_metadata либо UploadRejected: conversion_failed | Неверные query-параметры, повреждённый документ, пустое извлечение/разбиение. |
| 503 | detail: ingestion_unavailable | Технический сбой, неизвестный коммит, таймаут или неготовая зависимость. Приём не подтверждён. |

HTTP-ошибка не раскрывает исключения, секреты и внутренние пути. После начала приёма Gateway пишет revision_id/job_id в диагностическое событие. При неизвестном коммите сохраняет временные данные и изображения: Worker мог уже получить задание. Потеря ответа не доказывает отсутствие регистрации. Автоматического повтора, ключа идемпотентности и дедупликации запросов нет; повторный POST создаёт новый revision_id. Исход выясняет оператор по job_id в диагностике и хранилище перед повтором.

## Запуск

Из `app/`: `python -m uvicorn rag.ingestion.application:app --host 127.0.0.1 --port 8001 --no-access-log`. Образец настроек — `.env.example` в корне репозитория. Приложение запускает/закрывает Runtime и настроенные роли OCR/Vision Model Gateway через lifespan, без соединений и загрузки моделей при импорте. Миграции, подготовка хранилищ, Queue Relay и Worker запускаются отдельно по [правилам Runtime](../infrastructure/runtime-entrypoints.md).

## Связанные контракты

[Gateway](./gateway.md) · [DTO](./contracts.md) · [Document Converter](./converter.md) · [PostgreSQL](../infrastructure/postgresql/ingestion.md) · [Upload spool](../infrastructure/upload-spool/contracts.md)
