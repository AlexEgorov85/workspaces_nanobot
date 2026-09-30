"""Capability ``llm`` — сервис вызова провайдера.

Слой делает две вещи и только две: проверяет аргументы на границе запроса и
переводит сбой провайдера в доменную ошибку платформы. Всё, что ниже —
в ``libs/llm``: и адрес эндпойнта, и заголовки, и retry-цикл.

**Ключ провайдера и адрес приходят только из конфигурации.** Ни одна операция
не принимает URL, заголовки или тело запроса аргументом: адрес собирается
владельцем из своего конфига, и подсунуть провайдеру свой endpoint через
вызов операции нельзя.

**Профиль вызова.** Операция обслуживает инфраструктуру (генерация SQL в
capability ``audit``, skill-конвейер), а не модель. Вызов от профиля
``model`` отклоняется, и это не формальность: инструмент, которым модель
может дёрнуть LLM, позволяет модели вызвать саму себя и оплатить этот вызов.
Серверная фильтрация списка инструментов тут не нужна и не делается —
профили фильтруются на стороне агента (см. ``tags``/``permissions`` в
объявлении операции и ``EnterpriseMcpSettings`` агента).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from libs.enterprise_common.errors import (
    EnterpriseError,
    InfrastructureError,
    InvalidRequestError,
)
from libs.llm.client import call_llm
from libs.llm.config import LlmConfig, resolve_llm_config
from libs.llm.embeddings import get_embedding

#: Профили вызывающей стороны. Значения совпадают с capability ``data``:
#: словарь профилей должен стать общим, но его перенос тронул бы чужой файл,
#: поэтому набор продублирован, а расхождение ловится тестом контракта.
AUDIENCE_MODEL = "model"
AUDIENCE_RUNTIME = "runtime"

#: Границы параметров запроса. Не «на вкус», а из контракта провайдера:
#: ``temperature`` выше 2 и неположительный ``max_tokens`` либо отвергаются
#: сервером провайдера, либо молча меняют стоимость вызова.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_MAX_TOKENS = 1
#: Нулевой таймаут — это «запрос не уйдёт никогда», а не «быстрый ответ».
MIN_TIMEOUT = math.nextafter(0.0, 1.0)


@dataclass(frozen=True)
class CompletionResult:
    """Доменный результат вызова: текст ответа и фактическая модель.

    Модель возвращается явно: переопределение ``model`` аргументом приходит
    из capability ``audit``, и в журнале должно быть видно, к какой модели
    ушёл запрос.
    """

    text: str
    model: str


@dataclass(frozen=True)
class EmbeddingResult:
    """Доменный результат векторизации: вектор, размерность и модель.

    Размерность отдаётся явно, потому что она участвует в подписи индекса
    (``libs.vectors.signature``): индекс, построенный на векторах другой
    размерности, должен быть опознан как устаревший, а не молча отдавать
    выдачу с бессмысленными score.
    """

    vector: tuple[float, ...]
    dimension: int
    model: str


class LlmService:
    """Вызов настроенного провайдера. HTTP-клиент принадлежит ``libs/llm``."""

    def __init__(
        self,
        *,
        config: LlmConfig | Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        call: Callable[..., str] | None = None,
        embed: Callable[..., list[float]] | None = None,
    ) -> None:
        """
        Args:
            config: готовый конфиг провайдера.
            env: источник окружения для резолва (тестовый seam).
            call: функция вызова. По умолчанию ``libs.llm.client.call_llm``;
                параметр существует, чтобы сервис тестировался без сети.
            embed: функция векторизации. По умолчанию
                ``libs.llm.embeddings.get_embedding`` — тот же seam, чтобы
                ``embed`` тестировался без сети. Сам HTTP остаётся в
                ``libs/llm``: сервис его не создаёт, он уже принадлежит слою.

        Конфиг **не резолвится в конструкторе, сознательно.** Один процесс
        обслуживает несколько capability, и недонастроенная ``llm`` не должна
        снимать из работы ``data``: ``history_search`` работает и без
        провайдера. Fail-fast в конструкторе означал бы, что забытый ключ в
        окружении уронил весь сервер вместе с операциями, которых он не
        касается, а диагностика указывала бы на процесс, а не на capability.

        Отказ не спрятан: первая операция получает ``infrastructure_error`` с
        именем недостающей переменной (его даёт ``resolve_llm_config``), а
        :meth:`describe` показывает capability как ненастроенную.
        """
        self._cfg: LlmConfig | None = (
            None if config is None else LlmConfig.from_mapping(config)
        )
        self._env = env
        self._call = call or call_llm
        self._embed = embed or get_embedding

    @property
    def config(self) -> LlmConfig:
        """Конфигурация провайдера (без печати ключа в лог).

        Ленивое свойство: при первом обращении резолвит окружение. Отсутствие
        конфигурации — ``InfrastructureError``, а не ``None``: вызывающая
        сторона обязана отличать «провайдер не настроен» от «провайдер молчит».
        """
        if self._cfg is None:
            self._cfg = resolve_llm_config(env=self._env)
        return self._cfg

    @property
    def is_configured(self) -> bool:
        """Настроена ли capability. Не бросает: используется в health-отчёте."""
        if self._cfg is not None:
            return True
        try:
            return self.config is not None
        except InfrastructureError:
            return False

    def describe(self) -> dict[str, Any]:
        """Сведения для баннера запуска и health-отчёта. Без ``api_key``.

        Не бросает при неполной конфигурации: баннер запуска обязан показать
        состояние capability, а не упасть из-за него.
        """
        if not self.is_configured:
            return {
                "configured": False,
                "provider": None,
                "model": None,
                "api_base": None,
                "key_configured": False,
            }
        cfg = self.config
        return {
            "configured": True,
            "provider": cfg.provider,
            "model": cfg.model,
            "api_base": cfg.api_base,
            "key_configured": bool(cfg.api_key),
        }

    # -- операции -----------------------------------------------------------

    def embed(
        self,
        *,
        text: str,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        audience: str = AUDIENCE_RUNTIME,
    ) -> EmbeddingResult:
        """Вернуть эмбеддинг текста.

        Обслуживает capability ``vectors``: HTTP-вызов провайдера принадлежит
        этому слою, поэтому векторизация идёт через сервис из контейнера, а не
        собственным клиентом владельца индекса.

        Args:
            text: текст для векторизации; пустой — ошибка аргументов.
            model: переопределение модели эмбеддингов. ``None`` — модель из
                конфига: подмена молчала бы дала вектор той размерности, на
                которой индекс не строили.
            max_retries/timeout: ретраи и таймаут, как у клиента.
            audience: профиль вызывающей стороны.

        Returns:
            :class:`EmbeddingResult` с вектором и его размерностью.

        Raises:
            InvalidRequestError: некорректные аргументы или вызов от модели.
            InfrastructureError: провайдер не ответил либо вернул пустой вектор.
        """
        self._require_runtime(audience, "embed")
        # Через свойство, а не через self._cfg: при сборке сервера
        # конфиг не передан, и ленивый резолв живёт именно здесь.
        # Прямое чтение self._cfg давало cfg=None в клиент и
        # AttributeError на .model в конце операции.
        cfg = self.config

        body = _require_text(text, "text")
        model_name = _optional_text(model, "model")
        if max_retries is None:
            raise InvalidRequestError("max_retries обязателен, получено None")
        attempts = _int_arg(max_retries, "max_retries", minimum=0)
        wait = _float_arg(timeout, "timeout", minimum=MIN_TIMEOUT)

        try:
            vector = self._embed(
                body,
                cfg=cfg,
                model=model_name,
                max_retries=attempts,
                timeout=wait,
            )
        except EnterpriseError:
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(f"провайдер не вернул эмбеддинг: {exc}") from exc

        if not vector:
            raise InfrastructureError("провайдер вернул пустой эмбеддинг")
        values = tuple(float(value) for value in vector)
        return EmbeddingResult(
            vector=values,
            dimension=len(values),
            model=model_name or cfg.model,
        )

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        audience: str = AUDIENCE_RUNTIME,
    ) -> CompletionResult:
        """Вызвать провайдера и вернуть текст ответа.

        Args:
            prompt: текст запроса; пустой — ошибка аргументов, а не вызов
                «на всякий случай».
            system: необязательная системная инструкция.
            context: история диалога, добавляется перед ``prompt``.
            model/max_tokens/temperature: переопределение параметров запроса.
            max_retries/timeout: повторы и таймаут, как в клиенте.
            audience: профиль вызывающей стороны.

        Returns:
            :class:`CompletionResult`.

        Raises:
            InvalidRequestError: некорректные аргументы или вызов от модели.
            InfrastructureError: провайдер не ответил либо ответил пустотой.
        """
        self._require_runtime(audience, "complete")
        # См. комментарий в embed(): конфиг берётся лениво, один раз
        # на операцию, и тот же объект уходит в клиент.
        cfg = self.config

        text = _require_text(prompt, "prompt")
        instruction = _require_text(system, "system", allow_empty=True)
        if context is not None:
            _require_messages(context, "context")

        model_name = _optional_text(model, "model")
        limit = _int_arg(max_tokens, "max_tokens", minimum=MIN_MAX_TOKENS)
        heat = _optional_temperature(temperature)
        if max_retries is None:
            # ``None`` на проводе MCP означает «параметр не задан», а дефолт
            # 3 попытки не восстановить молча: отключение ретраев не должно
            # выглядеть как «провайдер перестал отвечать».
            raise InvalidRequestError("max_retries обязателен, получено None")
        attempts = _int_arg(max_retries, "max_retries", minimum=0)
        wait = _float_arg(timeout, "timeout", minimum=MIN_TIMEOUT)

        messages: list[dict[str, Any]] = []
        if instruction:
            messages.append({"role": "system", "content": instruction})
        messages.append({"role": "user", "content": text})

        try:
            answer = self._call(
                messages,
                cfg=cfg,
                context=context,
                model=model_name,
                max_tokens=limit,
                temperature=heat,
                max_retries=attempts,
                timeout=wait,
            )
        except EnterpriseError:
            # Доменная ошибка клиента уже осмысленна: повторная обёртка
            # потеряла бы её код, и вызывающая сторона решила бы, что
            # провайдер сломался, когда на самом деле плохие аргументы.
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(f"провайдер LLM не ответил: {exc}") from exc

        if not answer or not answer.strip():
            raise InfrastructureError("провайдер LLM вернул пустой ответ")
        return CompletionResult(text=answer, model=model_name or cfg.model)

    # -- проверки -----------------------------------------------------------

    def _require_runtime(self, audience: str, operation: str) -> None:
        """Отклонить вызов от профиля модели.

        Модель, получившая инструмент «спросить LLM», может вызвать сама себя:
        вызов не ограничен по стоимости и не виден в счёте как отдельная
        задача. Отказ здесь, а не фильтрация списка инструментов, потому что
        фильтрация живёт на стороне агента, а этот сервис — последний рубеж.
        """
        if audience != AUDIENCE_RUNTIME:
            raise InvalidRequestError(
                f"{operation} доступна только рантайму агента, профиль вызова: {audience}"
            )


def _require_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    """Проверить, что аргумент — строка; при необходимости — непустая."""
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} должен быть строкой, получено {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise InvalidRequestError(f"{name} не должен быть пустым")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    """Проверить необязательную строку: ``None`` — не задано."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} должен быть строкой, получено {type(value).__name__}")
    if not value.strip():
        raise InvalidRequestError(f"{name} не должен быть пустым")
    return value


def _require_messages(value: Any, name: str) -> None:
    """Проверить историю чата: список сообщений ``{role, content}``.

    Проверяется форма, а не содержимое: провайдер вернёт 400 на мусоре, и
    ошибка от него неотличима от сбоя сети, а это разные коды домена.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidRequestError(
            f"{name} должен быть списком сообщений, получено {type(value).__name__}"
        )
    for index, message in enumerate(value):
        if not isinstance(message, Mapping):
            raise InvalidRequestError(
                f"{name}[{index}] должен быть объектом с role и content, "
                f"получено {type(message).__name__}"
            )
        for field in ("role", "content"):
            if not isinstance(message.get(field), str):
                raise InvalidRequestError(
                    f"{name}[{index}].{field} должен быть строкой, получено "
                    f"{type(message.get(field)).__name__}"
                )


def _int_arg(value: Any, name: str, *, minimum: int) -> int | None:
    """Проверить необязательное целое снизу.

    ``None`` означает «не задано» — значение по умолчанию берёт клиент из
    конфига. ``bool`` отвергается явно: ``True`` проходит
    ``isinstance(x, int)`` и превратился бы в ``max_tokens=1`` — тихий ответ
    нулевой длины вместо ошибки аргументов.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} должен быть целым числом, получено {value!r}")
    if value < minimum:
        raise InvalidRequestError(f"{name} должен быть не меньше {minimum}, получено {value}")
    return value


def _float_arg(value: Any, name: str, *, minimum: float) -> float:
    """Проверить обязательное число снизу, отвергнув не-числа и nan/inf.

    ``nan`` прошёл бы сравнение ``< minimum`` и ушёл бы в HTTP-клиент как
    таймаут, который не наступает никогда.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"{name} должен быть числом, получено {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidRequestError(f"{name} должен быть конечным числом, получено {value!r}")
    if number < minimum:
        raise InvalidRequestError(f"{name} должен быть не меньше {minimum}, получено {value}")
    return number


def _optional_temperature(value: Any) -> float | None:
    """Проверить необязательную температуру в диапазоне провайдера."""
    if value is None:
        return None
    number = _float_arg(value, "temperature", minimum=MIN_TEMPERATURE)
    if number > MAX_TEMPERATURE:
        raise InvalidRequestError(
            f"temperature должна быть в диапазоне [{MIN_TEMPERATURE}, {MAX_TEMPERATURE}], "
            f"получено {value!r}"
        )
    return number
