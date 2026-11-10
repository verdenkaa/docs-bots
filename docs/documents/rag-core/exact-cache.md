---
title: Exact Cache
sidebar_position: 5
---

:::warning Рабочий контракт
Чтение и запись заданы для MVP. Срок жизни записи и лимит памяти уточняются.
:::

**[Точки входа Exact Cache](./exact-cache-entrypoints.md)** — сигнатуры, параметры, форматы результата и примеры.

## Чтение и запись

Exact Cache вызывает `read_cached_answer(query)` и сравнивает поколение из поля `value` DTO `CacheRead` с переданным Core. При совпадении возвращает только `answer_text` и `sources`. Для старого значения вызывает `delete_cached_answer_if_unchanged(query, compare_token)`, для более нового возвращает промах. При записи передаёт `CachedAnswer` в `write_cached_answer_if_not_older`. Чтение, удаление и запись ограничиваем по времени. Сбой кеша не задерживает основной путь сверх этого бюджета. [Функции Redis](../infrastructure/redis/contracts.md)

Core сохраняет в кеш только новый ответ с источниками после успешного коммита. При попадании в кеш Core сохраняет готовый итог для своего запроса и сразу переходит к доставке. Для теста или запуска без кеша достаточно подставить `get_answer` с постоянным промахом и `store_answer` без записи. [DTO результата](./contracts.md#общие-dto) · [Формат записи и правила Redis](../infrastructure/redis/index.md)
