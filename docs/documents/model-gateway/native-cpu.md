---
title: CPU-модели OCR и reranker
sidebar_position: 8
---

:::warning Проверка профиля
Набор выбран для CPU-сервера. Скорость и качество на документах команды требуют проверки.
:::

Внутренний provider `native_cpu` вызывает отдельный сервис `native-models` в сети Compose. Публичные DTO и операции Model Gateway сохраняются. SDK PaddleOCR/PaddlePaddle и Transformers/PyTorch установлены в отдельном образе, чтобы их зависимости не меняли lock приложения. Модельные вызовы приложения выполняются только через Model Gateway; сервис не является публичным пользовательским API.

## Модели

| Роль | Модель и нормализация |
| --- | --- |
| OCR | PaddleOCR PP-OCRv5: официальный `PP-OCRv5_server_det` и `eslav_PP-OCRv5_mobile_rec` для русского и английского текста. Вход RGB переводится в BGR, автоматическая ориентация/разворачивание отключены. Результат — строки распознанного текста, пустой результат допустим. |
| Reranker | `BAAI/bge-reranker-v2-m3`, CPU float32. Пары вопрос/фрагмент ограничены 1024 токенами; sigmoid одного logit даёт score. Все исходные индексы сохраняются. |

Коммиты и состав OCR зафиксированы в `deploy/model-profile.json`. Hugging Face snapshot загружается по immutable revision, затем SDK открывает локальные файлы с `local_files_only=True`, `trust_remote_code=False`; PyTorch читает safetensors. OCR получает явные локальные каталоги обеих моделей. Сервис не выбирает альтернативную модель при ошибке.

[Reranker](https://huggingface.co/BAAI/bge-reranker-v2-m3) · [PP-OCRv5 и языки](https://www.paddleocr.ai/v3.3.2/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html). Защиту от инъекций выполняет отдельный [существующий Horizon ONNX-сервис](./injection-detector.md); его профиль, формат оценки и порог не смешиваются с native OCR/reranker.

## <span className="operation-badge operation-badge--call">CALL</span> `register_providers`

~~~python
def register_providers() -> None: ...
~~~

`MODEL_GATEWAY_PROVIDER_MODULES` должен включать `rag.model_gateway.native_cpu`. Binding задаёт `provider="native_cpu"`, `execution="remote"`, `endpoint="http://native-models:8090"`, `credential_env="NATIVE_MODEL_TOKEN"`. Значение секрета хранится только в env.

`start_models` сверяет model_id, model_revision и profile_id с загруженным backend и выполняет обычную реальную role-specific пробу. Приватный HTTP передаёт нормализованный payload; бинарное изображение кодируется base64. Неверная версия, повреждённый ответ, таймаут и недоступный сервис дают обычные StepError без вывода исходного текста, изображения, HTTP-ответа или секрета. `close_models` закрывает клиент и не останавливает общий сервер.

В серверной конфигурации `COMPOSE_PROFILES=native` включает SDK-сервис. Модели сохраняются на отдельном томе `native_model_data`. Сервис загружает только роли `native_cpu` из bindings; веса и HTTP не стартуют при импорте Python-модулей. Подготовка `models-init` ждёт готовности сервиса. Порт 8090 не публикуется на хост.
