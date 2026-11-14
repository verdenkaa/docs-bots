---
title: "GET /sources/{document_id}"
sidebar_position: 5
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--get">GET</span> `/sources/{document_id}`

## Назначение и доступ

Браузер по ссылке источника из ответа. Проектный выбор MVP — публичная заглушка только с названием; оригиналы, изображения и тикеты через неё не выдаются.

## Что принимает

| Поле | Тип | Смысл / пример |
| --- | --- | --- |
| document_id | UUID в пути | Идентичность документа из SourceRef. |

Тела и query-параметров нет.

## Бизнес-логика

1. Разобрать UUID.
2. Вызвать Runtime.load_source_metadata(document_id).
3. Экранировать source_name и отдать HTML. Не загружать Qdrant, оригинал или чужое имя из query.

## Что отдаёт

200 OK, Content-Type: text/html; charset=utf-8. Текст страницы: «Тут должен быть хостинг документа „Реестр мебели“». Ссылка ведёт на документ целиком, без якорей.

## Ошибки и повторы

| HTTP | Причина | Действие |
| --- | --- | --- |
| 422 | Некорректный UUID. | Проверить ссылку. |
| 404 | Неизвестный источник. | Источник отсутствует. |
| 503 | Метаданные недоступны. | Повторить позже. |

## Связанные контракты

[load_source_metadata](../../infrastructure/media.md#load-source-metadata) · [SourceRef](../../rag-core/contracts.md#общие-dto)
