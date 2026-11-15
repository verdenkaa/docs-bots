---
title: "Mini App — новые DTO"
sidebar_position: 12
---

Контракты нового HTTP-канала находятся в `rag.rag_api.mini_app_contracts`.
Они не заменяют `NewQuestion`, `Principal`, `QuestionInput`, `ClaimedQuestion`,
`QuestionOutcome` или сообщения RabbitMQ. [HTTP API и запуск](./mini-app.md).

~~~python
from typing import Annotated, Literal
from pydantic import Field, StringConstraints
from rag.dto import DTO, PositiveId

class MiniAppIdentity(DTO):
    user_id: PositiveId
    username: str | None = None
    role: Literal['user', 'admin'] = 'user'

class WebQuestionInput(DTO):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]

class WebSupportInput(DTO):
    reason: Literal['dissatisfied', 'human_request']

class WebAccessInput(DTO):
    enabled: bool = Field(strict=True)
~~~

`MiniAppIdentity` создаёт только доверенный адаптер после проверки подписи
Telegram. Она не принимается из JSON клиента, не является прежним `Principal`
и не позволяет войти в старые Basic-ручки. `user_id` определяет владельца;
`username` нормализуется и определяет допуск новых вопросов. Роль `admin`
назначается только через `MINI_APP_ADMIN_IDS`.

`WebQuestionInput` принимает только текст: неизвестные поля, включая `image`,
`upload_id`, `user_id`, `username`, `role`, запрещены базовым DTO. Общий размер
HTTP JSON ограничен 32 KiB, независимо от символьного ограничения.
Оставленная в интерфейсе кнопка изображений отключена. `POST /api/v1/uploads`
возвращает 501; OCR и расширение общего DTO отложены. Изображения в **ответах**
продолжают использовать существующие `ImageRef`, S3 и защищённую галерею.

Остальные ответы веб-ручек используют JSON существующих `QuestionOutcome`,
`SupportCase`, `TicketView`, `TicketMessage`, `AccessRecord`, `MetricsSnapshot`
в описанных HTTP-обёртках. Старые формы этих DTO не меняются.
