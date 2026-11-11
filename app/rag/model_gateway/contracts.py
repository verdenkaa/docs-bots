"""Contracts: docs/documents/model-gateway/contracts.md."""

from pydantic import Field, model_validator
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText

from typing import Annotated, Literal, Self


ModelRole = Literal["embedding", "reranker", "injection", "generation"]


class ModelBinding(DTO):
    """Настройка выбранного локального или удалённого провайдера одной роли модели.

    Содержит имя переменной секрета, но не сам секрет; Runtime применяет настройку при старте.
    """

    profile_id: NonEmptyText = Field(description="ID конфигурации модели; отличает несовместимые настройки и векторы.")
    execution: Literal["local", "remote"] = Field(
        description="local — модель в процессе; remote — обращение к серверу провайдера.",
    )
    provider: NonEmptyText = Field(description="Ключ адаптера провайдера; конкретные значения выбираются после кейса.")
    model_id: NonEmptyText = Field(description="Имя модели для выбранного провайдера.")
    model_revision: NonEmptyText = Field(description="Зафиксированная редакция модели для воспроизводимого профиля.")
    endpoint: str | None = Field(description="Адрес API; обязателен для remote, для local допустим None.")
    credential_env: str | None = Field(
        description="Имя переменной окружения с секретом; None, если авторизация не нужна.",
    )
    max_concurrency: PositiveId = Field(
        description="Положительный предел одновременно выполняемых вызовов этого binding.",
    )
    embedding_dimensions: PositiveId | None = Field(
        description="Ожидаемая размерность эмбеддинга; None для ролей без векторов.",
    )

    @model_validator(mode="after")
    def check_binding(self) -> Self:
        """Проверить обязательность endpoint у удалённого провайдера; вернуть self или ValueError."""
        if self.execution == "remote" and not self.endpoint:
            raise ValueError("remote binding requires endpoint")
        return self


class EmbeddingResult(DTO):
    """Векторы в порядке входных текстов и профиль, которым они рассчитаны."""

    profile_id: NonEmptyText = Field(description="ID конфигурации модели; отличает несовместимые настройки и векторы.")
    vectors: list[list[float]] = Field(description="Один конечный вектор на входной текст, в исходном порядке.")


class RerankScore(DTO):
    """Оценка кандидата по его индексу во входном списке; сортирует Searcher."""

    candidate_index: NonNegativeInt = Field(description="Индекс кандидата с 0 во входном списке; не ID чанка.")
    score: float = Field(description="Оценка модели или поиска; шкала определяется конкретным профилем.")


class RerankResult(DTO):
    """Оценки реранкера без бизнес-фильтрации кандидатов."""

    profile_id: NonEmptyText = Field(description="ID конфигурации модели; отличает несовместимые настройки и векторы.")
    scores: list[RerankScore] = Field(description="Оценки кандидатов с исходными индексами для сортировки Searcher.")


class InjectionResult(DTO):
    """Наблюдение модели защиты; решение и порог принадлежат Guardrails.

    Нужен хотя бы label или score; score не обязан быть вероятностью.
    """

    profile_id: NonEmptyText = Field(description="ID конфигурации модели; отличает несовместимые настройки и векторы.")
    label: Literal["benign", "injection"] | None = Field(
        description="Метка модели: benign или injection; None, если провайдер вернул только score.",
    )
    score: float | None = Field(description="Оценка модели или поиска; шкала определяется конкретным профилем.")

    @model_validator(mode="after")
    def require_observation(self) -> Self:
        """Потребовать label или score; вернуть self или поднять ValueError."""
        if self.label is None and self.score is None:
            raise ValueError("label or score is required")
        return self


class ModelMessage(DTO):
    """Одно сообщение диалога, передаваемого провайдеру генерации."""

    role: Literal["system", "user", "assistant"] = Field(description="Роль сообщения в диалоге провайдера генерации.")
    content: NonEmptyText = Field(description="Содержимое сообщения модели.")


class GenerationOptions(DTO):
    """Параметры генерации, независимые от конкретного SDK провайдера."""

    max_output_tokens: PositiveId = Field(
        description="Положительный бюджет выходных токенов; не число символов Telegram.",
    )
    temperature: Annotated[float, Field(ge=0)] | None = Field(
        description="Неотрицательная температура; None оставляет параметр провайдеру.",
    )


class TokenUsage(DTO):
    """Доступные счётчики провайдера; неизвестное значение — None, а не ноль."""

    input_tokens: NonNegativeInt | None = Field(
        description="Число входных токенов по данным провайдера; None — неизвестно.",
    )
    output_tokens: NonNegativeInt | None = Field(
        description="Число выходных токенов по данным провайдера; None — неизвестно.",
    )


class ModelGenerationResult(DTO):
    """Собранный ответ модели и причина остановки, ещё без бизнес-проверки цитат."""

    profile_id: NonEmptyText = Field(description="ID конфигурации модели; отличает несовместимые настройки и векторы.")
    text: str = Field(description="Собранный текст модели; для completed обязателен непустой текст.")
    finish_reason: Literal["completed", "length", "filtered"] = Field(
        description="completed — завершён; length — исчерпан бюджет; filtered — остановлен фильтром.",
    )
    usage: TokenUsage | None = Field(description="Счётчики токенов; None, если провайдер их не сообщает.")

    @model_validator(mode="after")
    def require_completed_text(self) -> Self:
        """Потребовать непустой текст при completed; вернуть self или поднять ValueError."""
        if self.finish_reason == "completed" and not self.text.strip():
            raise ValueError("completed generation requires text")
        return self
