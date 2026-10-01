"""Capability ``llm`` — граница операций над внутренним сервисом LLM.

Слой делает две вещи и только две: проверяет аргументы **на границе запроса**
и переводит сбой провайдера в доменную ошибку платформы. Всё остальное —
в ``libs/llm``: и адрес эндпойнта, и заголовки, и retry-цикл, и резолв
настроек.

**Здесь нет бизнес-логики.** Сервис ничего не знает о том, зачем его зовут:
он не строит промпты, не разбирает доменный ответ, не решает, подходит ли
результат. Промпт приходит готовым и уходит готовым текстом. Правило не
косметическое — именно из-за «да заодно и разберём ответ» в слое LLM
появляется знание о чужих доменах, и смена домена начинает ломать правку
провайдера. Граница домена — это capability, владеющая доменом
(``audit``, ``data``, ``vectors``); ``llm`` обслуживает их, но не знает ни
одной.

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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from libs.enterprise_common.errors import InvalidRequestError
from libs.llm.config import LlmConfig
from libs.llm.gateway import (
    LlmGateway,
    optional_text,
    require_messages,
    require_text,
)
from libs.llm.gateway import gateway as llm_gateway

#: Профили вызывающей стороны. Значения совпадают с capability ``data``:
#: словарь профилей должен стать общим, но его перенос тронул бы чужой файл,
#: поэтому набор продублирован, а расхождение ловится тестом контракта.
AUDIENCE_MODEL = "model"
AUDIENCE_RUNTIME = "runtime"


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
    """Граница capability над сервисом общения с LLM.

    Настройки, HTTP-вызов и разбор ответа — не здесь, а в
    :class:`libs.llm.gateway.LlmGateway`. Здесь остаётся ровно то, что видит
    вызывающая сторона: проверка аргументов протокола, запрет вызова от
    профиля модели и доменные типы результата.
    """

    def __init__(
        self,
        *,
        config: LlmConfig | Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        call: Any = None,
        embed: Any = None,
        service: LlmGateway | None = None,
    ) -> None:
        """
        Args:
            config: готовый конфиг провайдера.
            env: источник окружения для резолва (тестовый seam).
            call: подмена вызова провайдера вместо
                ``libs.llm.client.call_llm`` — чтобы сервис тестировался без
                сети. Сам HTTP остаётся в ``libs/llm``: capability его не
                создаёт, он уже принадлежит слою-владельцу.
            embed: подмена векторизации вместо
                ``libs.llm.embeddings.get_embedding`` — тот же seam.
            service: готовый сервис целиком. Сборка сервера не передаёт его
                (сервис и так один на процесс), но он позволяет подложить
            целый сервис в тест, не перечисляя четыре seam'а.

        Конфиг **не резолвится в конструкторе, сознательно.** Один процесс
        обслуживает несколько capability, и недонастроенная ``llm`` не должна
        снимать из работы ``data``: ``history_search`` работает и без
        провайдера. Fail-fast в конструкторе означал бы, что забытый ключ
        уронил весь сервер вместе с операциями, которых он не касается, а
        диагностика указывала бы на процесс, а не на capability.

        Отказ не спрятан: первая операция получает ``infrastructure_error`` с
        именем недостающей настройки, а :meth:`describe` показывает capability
        как ненастроенную.
        """
        self._gateway = service or _gateway_for(
            config=config, env=env, call=call, embed=embed
        )

    @property
    def service(self) -> LlmGateway:
        """Сервис общения с LLM под этим capability."""
        return self._gateway

    @property
    def config(self) -> LlmConfig:
        """Конфигурация провайдера (без печати ключа в лог).

        Отсутствие конфигурации — ``InfrastructureError``, а не ``None``:
        вызывающая сторона обязана отличать «провайдер не настроен» от
        «провайдер молчит», иначе она ретраила бы то, что не починится
        повтором.
        """
        return self._gateway.config

    @property
    def is_configured(self) -> bool:
        """Настроена ли capability. Не бросает: используется в health-отчёте."""
        return self._gateway.is_configured

    def describe(self) -> dict[str, Any]:
        """Сведения для баннера запуска и health-отчёта. Без ``api_key``.

        Не бросает при неполной конфигурации: баннер запуска обязан показать
        состояние capability, а не упасть из-за него.
        """
        return self._gateway.describe()

    # -- операции -----------------------------------------------------------

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
        text = self._ask(
            prompt,
            system=system,
            context=context,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
        )
        return CompletionResult(
            text=text, model=optional_text(model, "model") or self.config.model
        )

    def send(
        self,
        *,
        messages: list[dict[str, Any]],
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        audience: str = AUDIENCE_RUNTIME,
    ) -> CompletionResult:
        """Вызвать провайдера готовым списком сообщений.

        Нужен вызывающим, у которых история собирается ими: конвейер
        генерации SQL дописывает к предыдущей попытке текст ошибки, и
        подгонять это под «один промпт» означало бы терять переписку.
        Граница протокола та же, что у :meth:`complete`, и промпт на проводе
        не меняется — меняется только способ его собрать.

        Args:
            messages: сообщения ``{role, content}``; форма проверяется, и
                содержимое — тоже: ответ на мусор от провайдера неотличим от
                сбоя сети, а это разные коды домена.
            context/max_tokens/temperature/max_retries/timeout/audience: как в
                :meth:`complete`.

        Raises:
            InvalidRequestError: некорректные аргументы или вызов от модели.
            InfrastructureError: провайдер не ответил либо ответил пустотой.
        """
        self._require_runtime(audience, "send")
        require_messages(messages, "messages")
        text = self._service_call(
            messages,
            context=context,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
        )
        return CompletionResult(
            text=text, model=optional_text(model, "model") or self.config.model
        )

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
        слою-владельцу, поэтому векторизация идёт через сервис из
        контейнера, а не собственным клиентом владельца индекса.

        Args:
            text: текст для векторизации; пустой — ошибка аргументов.
            model: переопределение модели эмбеддингов. ``None`` — модель из
                настроек сервиса.
            max_retries/timeout: ретраи и таймаут, как у клиента.
            audience: профиль вызывающей стороны.

        Returns:
            :class:`EmbeddingResult` с вектором и его размерностью.

        Raises:
            InvalidRequestError: некорректные аргументы или вызов от модели.
            InfrastructureError: провайдер не ответил либо вернул пустой вектор.
        """
        self._require_runtime(audience, "embed")
        vector = self._service_embed(
            text,
            model=model,
            max_retries=max_retries,
            timeout=timeout,
        )
        values = tuple(float(value) for value in vector)
        return EmbeddingResult(
            vector=values,
            dimension=len(values),
            model=optional_text(model, "model") or self.config.model,
        )

    # -- переход к сервису --------------------------------------------------

    def _ask(self, prompt: str, **kwargs: Any) -> str:
        """Отправить один промпт через сервис-владелец."""
        _require_present(kwargs.get("max_retries"), "max_retries")
        _require_present(kwargs.get("timeout"), "timeout")
        return self._gateway.ask(prompt, **kwargs)

    def _service_call(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        """Отправить готовые сообщения через сервис-владелец."""
        _require_present(kwargs.get("max_retries"), "max_retries")
        _require_present(kwargs.get("timeout"), "timeout")
        return self._gateway.send(messages, **kwargs)

    def _service_embed(self, text: str, **kwargs: Any) -> list[float]:
        """Векторизовать через сервис-владелец."""
        _require_present(kwargs.get("max_retries"), "max_retries")
        _require_present(kwargs.get("timeout"), "timeout")
        return self._gateway.embed(text, **kwargs)

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


def _gateway_for(
    *,
    config: LlmConfig | Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
    call: Any,
    embed: Any,
) -> LlmGateway:
    """Сервис общения с LLM для этой capability.

    Все четыре seam'а пусты — берётся сервис процесса: так его и собирает
    сервер, и только он один знает, какая модель сейчас настроена. Хоть один
    непустой — собирается отдельный экземпляр: тесту нужны свои настройки и
    свои подмены вызовов, а молча делить с процессом настроенный иначе
    сервис значило бы проверять не то, что работает в бою.
    """
    if config is None and env is None and call is None and embed is None:
        return llm_gateway()
    return LlmGateway(config=config, env=env, call=call, embed=embed)


def _require_present(value: Any, name: str) -> None:
    """Отклонить ``None`` у обязательного параметра.

    На проводе MCP ``None`` означает «параметр не задан», и дефолт молча
    подставился бы: отключение ретраев или таймаута выглядело бы как
    «провайдер перестал отвечать», а не как «параметр не передан». Внутри
    платформы ``None`` дефолт означает честное «возьми у сервиса», поэтому
    на границе протокола эти два смысла разведены.
    """
    if value is None:
        raise InvalidRequestError(f"{name} обязателен, получено None")
