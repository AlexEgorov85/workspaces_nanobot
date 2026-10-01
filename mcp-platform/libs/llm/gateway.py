"""Внутренний сервис общения с LLM — единственный в процессе.

Зачем он, если уже есть ``LlmService`` capability
-------------------------------------------------

Capability-сервис отвечает на вопрос «что делать с запросом, пришедшим по
протоколу MCP»: проверить аргументы, отклонить вызов от профиля модели,
перевести сбой провайдера в доменную ошибку конверта. Это граница протокола,
и её правомерно видеть из ``servers/``.

Этот модуль отвечает на другой вопрос: «куда вообще ходить за ответом и с
какими настройками». Сегодня ответ на него был размазан по трём местам —
клиент требовал ``cfg``, каждый вызывающий резолвил конфиг сам, а
настройки приезжали из окружения процесса. Внешне это выглядело как
``call_llm(prompt, cfg=resolve_llm_config(env=...), ...)``: «простой метод
отправки и получения ответа» требовал от вызывающей стороны знать, где
настройки, — то есть ровно то, чего вызывающая сторона знать не должна.

Теперь метод простой::

    from libs import llm

    text = llm.ask("Сколько будет 2+2?")
    data = llm.ask_json("Верни JSON {\"n\": <число>}")
    vector = llm.embed("документ")
    llm.describe()          # что настроено, без ключа

Конфигурация приходит из реестра (``libs.enterprise_common.settings``) —
того же единственного читателя, что и всё остальное в процессе. Ключ,
адрес и модель живут в ``platform.json`` (секция ``llm``); окружение
процесса остаётся запасным источником, поэтому развёртывание, задающее
``ENTERPRISE_LLM_*`` вручную, продолжает работать.

Один экземпляр на процесс
--------------------------

:class:`LlmGateway` владеет разрешённой конфигурацией и HTTP-вызовом, то
есть тем же, ради чего существует. Два экземпляра — два ответа на вопрос
«какая модель сейчас настроена», и вопрос «почему один вызов ушёл с
``mxbai``, а другой с ``MiniMax``» получает два правдоподобных ответа.
Поэтому в процессе он один (:func:`gateway`), а сборка сервера отдаёт ему
тот самый ``Settings``, который уже разрешил всё остальное.

Ошибки — доменные, без ``httpx`` наружу
----------------------------------------

Наружу выходят ``InfrastructureError`` (провайдер не ответил) и
``InvalidRequestError`` (плохие аргументы). Причина не в красоте: по коду
ошибки вызывающая сторона решает, повторять ли попытку, и ``httpx`` на
проводе конверта означал бы ``internal_error`` вместо ``upstream_unavailable``.

Пакет импортируется без HTTP-библиотеки на верхнем уровне (см.
``libs/llm/client.py``): ``import libs.llm`` остаётся безопасным в процессе,
где сети нет, — тесты сервисов и проверка конфигурации при старте.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from libs.enterprise_common.errors import (
    EnterpriseError,
    InfrastructureError,
    InvalidRequestError,
)
from libs.llm.client import call_llm, call_llm_json, parse_json_object
from libs.llm.config import LlmConfig, resolve_llm_config
from libs.llm.embeddings import get_embedding

logger = logging.getLogger(__name__)

#: Значение по умолчанию для ``timeout``. ``None`` у клиента означает
#: «дефолт клиента» (60 секунд) — и это не то же самое, что 60: константа
#: живёт здесь, чтобы вызывающая сторона не писала магическое число.
DEFAULT_TIMEOUT_SEC = 60.0
#: Повторы при 429 / таймауте / обрыве. Как у агентского клиента, откуда
#: перенесено без изменения: три попытки — это «провайдер моргнул».
DEFAULT_MAX_RETRIES = 3

#: Минимальная температура. Выше ``MAX_TEMPERATURE`` провайдер отвергает
#: запрос, а неположительный ``max_tokens`` тихо превращается в ответ
#: нулевой длины. Границы — из контракта провайдера, а не из вкуса.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_MAX_TOKENS = 1


def require_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"{name} должен быть строкой, получено {type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise InvalidRequestError(f"{name} не должен быть пустым")
    return value


def optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"{name} должен быть строкой, получено {type(value).__name__}"
        )
    if not value.strip():
        raise InvalidRequestError(f"{name} не должен быть пустым")
    return value


def optional_int(value: Any, name: str, *, minimum: int) -> int | None:
    """Проверить необязательное целое снизу.

    ``None`` — «не задано», значение возьмёт конфиг. ``bool`` отвергается
    явно: ``True`` проходит ``isinstance(x, int)`` и стал бы
    ``max_tokens=1``, то есть тихим ответом нулевой длины.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(f"{name} должен быть целым числом, получено {value!r}")
    if value < minimum:
        raise InvalidRequestError(f"{name} должен быть не меньше {minimum}, получено {value}")
    return value


def optional_temperature(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"temperature должен быть числом, получено {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidRequestError("temperature должен быть конечным числом")
    if number < MIN_TEMPERATURE or number > MAX_TEMPERATURE:
        raise InvalidRequestError(
            f"temperature должен быть в пределах [{MIN_TEMPERATURE}, {MAX_TEMPERATURE}], "
            f"получено {number}"
        )
    return number


def optional_timeout(value: Any) -> float:
    """Проверить таймаут. ``None`` — дефолт клиента.

    ``nan`` прошёл бы сравнение ``< minimum`` и ушёл бы в HTTP-клиент как
    таймаут, который не наступает никогда.
    """
    if value is None:
        return DEFAULT_TIMEOUT_SEC
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequestError(f"timeout должен быть числом, получено {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidRequestError("timeout должен быть конечным числом")
    if number <= 0.0:
        raise InvalidRequestError(
            "timeout должен быть положительным: нулевой — это «запрос не уйдёт "
            "никогда», а не «быстрый ответ»"
        )
    return number


def optional_retries(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_RETRIES
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(
            f"max_retries должен быть целым числом, получено {value!r}"
        )
    if value < 0:
        raise InvalidRequestError(
            f"max_retries должен быть не меньше 0, получено {value}"
        )
    return value


def require_messages(messages: Any) -> list[dict[str, Any]]:
    """Проверить список сообщений ``{role, content}``.

    Проверяется форма, а не содержимое: провайдер вернёт 400 на мусоре, а
    его ошибка неотличима от сбоя сети, а это разные коды домена.
    """
    if isinstance(messages, (str, bytes)) or not isinstance(messages, (list, tuple)):
        raise InvalidRequestError(
            f"messages должен быть списком сообщений, получено {type(messages).__name__}"
        )
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise InvalidRequestError(
                f"messages[{index}] должен быть объектом с role и content, "
                f"получено {type(message).__name__}"
            )
        for field in ("role", "content"):
            if not isinstance(message.get(field), str):
                raise InvalidRequestError(
                    f"messages[{index}].{field} должен быть строкой, получено "
                    f"{type(message.get(field)).__name__}"
                )
        result.append(dict(message))
    return result


class LlmGateway:
    """Общение с провайдером: настройки, чат, JSON, эмбеддинги."""

    def __init__(
        self,
        *,
        settings: Any | None = None,
        env: Mapping[str, str] | None = None,
        config: LlmConfig | Mapping[str, Any] | None = None,
        call: Callable[..., str] | None = None,
        call_json: Callable[..., dict[str, Any] | None] | None = None,
        embed: Callable[..., list[float]] | None = None,
    ) -> None:
        """Собрать сервис.

        Args:
            settings: готовый реестр (``Settings``). Предпочтительный способ:
                значение приходит из того же разбора, что и всё остальное в
                процессе, и «откуда взялась настройка» у него одно.
            env: источник окружения вместо реестра. Только для тестов и для
                вызывающих, у которых реестра нет: второй разбор настроек
                рядом с реестром — это ровно то, что реестр и отменил.
            config: готовый конфиг провайдера — верхняя точка тестового
                подмены без реестра и без окружения.
            call / call_json / embed: подмены вызовов провайдера. HTTP
                остаётся в ``libs/llm``: сервис его не создаёт.

        Конфигурация **не резолвится в конструкторе.** Capability ``llm``
        необязательна: недонастроенный провайдер не должен снимать из работы
        ``data``, ``audit`` и ``vectors``. Отказ приходит на первом вызове —
        ``InfrastructureError`` с именем недостающей настройки, — а
        :meth:`describe` показывает сервис как ненастроенный.
        """
        self._settings = settings
        self._env = env
        self._cfg: LlmConfig | None = (
            None if config is None else LlmConfig.from_mapping(config)
        )
        self._call = call or call_llm
        self._call_json = call_json or call_llm_json
        self._embed = embed or get_embedding

    # -- настройки ---------------------------------------------------------

    def _resolved_env(self) -> Mapping[str, str]:
        """Значения настроек для резолва: реестр, иначе окружение.

        ``Settings`` читается один раз и кэшируется: иначе каждый вызов
        разбирал бы заново ``platform.json`` и ``.secrets.env`` с диска.
        """
        if self._settings is not None:
            return self._settings.as_env()
        if self._env is not None:
            return self._env
        from libs.enterprise_common.settings import Settings

        self._settings = Settings()
        return self._settings.as_env()

    @property
    def config(self) -> LlmConfig:
        """Конфигурация провайдера.

        Raises:
            InfrastructureError: не задана модель или ``api_base``. Подстановки
                дефолта нет: запрос к несуществующей модели неотличим от
                «модель молчит».
        """
        if self._cfg is None:
            self._cfg = resolve_llm_config(env=self._resolved_env())
        return self._cfg

    @property
    def is_configured(self) -> bool:
        """Настроен ли сервис. Не бросает — для баннера и health-отчёта."""
        if self._cfg is not None:
            return True
        try:
            return self.config is not None
        except InfrastructureError:
            return False

    def describe(self) -> dict[str, Any]:
        """Сведения о настройках. **Без ``api_key``** — только факт наличия.

        Не бросает при неполной конфигурации: баннер запуска обязан показать
        состояние, а не упасть из-за него.
        """
        if not self.is_configured:
            return {
                "configured": False,
                "provider": None,
                "model": None,
                "api_base": None,
                "key_configured": False,
                "max_tokens": None,
                "temperature": None,
                "embed": {
                    "model": None,
                    "api_base": None,
                    "key_configured": False,
                },
            }
        cfg = self.config
        return {
            "configured": True,
            "provider": cfg.provider,
            "model": cfg.model,
            "api_base": cfg.api_base,
            "key_configured": bool(cfg.api_key),
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "embed": {
                "model": cfg.embed_model or None,
                "api_base": (cfg.embed_api_base or cfg.api_base) or None,
                "key_configured": bool(cfg.embed_api_key or cfg.api_key),
            },
        }

    def sources(self) -> dict[str, str]:
        """Откуда взята каждая настройка провайдера.

        Единственный честный ответ на вопрос «я задал это в файле, а
        применилось ли оно»: окружение старше файла, поэтому значение из
        ``platform.json`` может быть перекрыто переменной процесса, и без
        этого ответа файл выглядит рабочим, не влияя ни на что.
        """
        if self._settings is None:
            return {}
        names = (
            "ENTERPRISE_LLM_PROVIDER",
            "ENTERPRISE_LLM_MODEL",
            "ENTERPRISE_LLM_API_BASE",
            "ENTERPRISE_LLM_API_KEY",
            "ENTERPRISE_LLM_MAX_TOKENS",
            "ENTERPRISE_LLM_TEMPERATURE",
            "ENTERPRISE_EMBED_API_BASE",
            "ENTERPRISE_EMBED_API_KEY",
            "ENTERPRISE_EMBED_MODEL",
        )
        return {name: self._settings.source(name) for name in names}

    # -- чат ---------------------------------------------------------------

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Отправить запрос и получить текст ответа. **Простой метод.**

        Args:
            prompt: текст запроса; пустой — ошибка аргументов, а не вызов
                «на всякий случай».
            system: необязательная системная инструкция.
            context: история диалога, добавляется перед ``prompt``.
            model/max_tokens/temperature: переопределение параметров запроса.
                ``None`` — значение из настроек сервиса.
            max_retries/timeout: ``None`` — дефолты сервиса.

        Returns:
            Текст ответа провайдера.

        Raises:
            InvalidRequestError: некорректные аргументы.
            InfrastructureError: провайдер не настроен или не ответил.
        """
        text = require_text(prompt, "prompt")
        instruction = require_text(system, "system", allow_empty=True)
        messages: list[dict[str, Any]] = []
        if instruction:
            messages.append({"role": "system", "content": instruction})
        messages.append({"role": "user", "content": text})
        return self.send(
            messages,
            context=context,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
        )

    def send(
        self,
        messages: list[dict[str, Any]],
        *,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """Вызвать провайдера готовым списком сообщений.

        ``ask`` — обёртка над этим методом. Нужен там, где сообщения
        собирает вызывающая сторона: конвейер генерации SQL сам дописывает
        предыдущую попытку и текст ошибки, и подгонять это под
        «один промпт» означало бы терять историю.
        """
        payload = require_messages(messages)
        if context is not None:
            require_messages(context)
        cfg = self.config
        model_name = optional_text(model, "model")
        limit = optional_int(max_tokens, "max_tokens", minimum=MIN_MAX_TOKENS)
        heat = optional_temperature(temperature)
        attempts = optional_retries(max_retries)
        wait = optional_timeout(timeout)

        try:
            answer = self._call(
                payload,
                cfg=cfg,
                context=list(context) if context is not None else None,
                model=model_name,
                max_tokens=limit,
                temperature=heat,
                max_retries=attempts,
                timeout=wait,
            )
        except EnterpriseError:
            raise
        except Exception as exc:  # noqa: BLE001 - наружу уходит доменная ошибка
            raise InfrastructureError(f"провайдер LLM не ответил: {exc}") from exc

        if not answer or not answer.strip():
            raise InfrastructureError("провайдер LLM вернул пустой ответ")
        return answer

    def ask_json(
        self,
        prompt: str,
        *,
        system: str = "",
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int | None = 0,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """Как :meth:`ask`, но ответ разбирается как JSON-объект.

        ``None`` — ответ не разобрался. Исключение **не** пробрасывается:
        разбор ответа модели — это её формат выдачи, а не сбой сервиса, и
        вызывающая сторона решает по-своему, пробовать ли ещё раз.
        """
        try:
            text = self.ask(
                prompt,
                system=system,
                context=context,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                max_retries=max_retries,
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 - формат ответа, а не отказ сервиса
            return None
        return parse_json_object(text)

    def send_json(
        self,
        messages: list[dict[str, Any]],
        *,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int | None = 0,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """Как :meth:`send`, но ответ разбирается как JSON-объект."""
        payload = require_messages(messages)
        try:
            return self._call_json(
                payload,
                cfg=self.config,
                context=list(context) if context is not None else None,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                max_retries=max_retries,
                timeout=timeout,
            )
        except EnterpriseError:
            raise
        except Exception:  # noqa: BLE001 - формат ответа, а не отказ сервиса
            return None

    # -- эмбеддинги --------------------------------------------------------

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> list[float]:
        """Вернуть эмбеддинг текста.

        Адрес, ключ и модель эмбеддера — свои (``ENTERPRISE_EMBED_*``), и
        пустые наследуют настройки чата: конфигурация с одним провайдером не
        должна требовать лишних переменных. Модель по умолчанию тоже своя
        (не чатная): уход в эндпойнт эмбеддингов с именем модели чата вернул
        бы вектор не той размерности, и подпись индекса разошлась бы с
        фактически использованной моделью.
        """
        body = require_text(text, "text")
        model_name = optional_text(model, "model")
        attempts = optional_retries(max_retries)
        wait = optional_timeout(timeout)

        try:
            vector = self._embed(
                body,
                cfg=self.config,
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
        return [float(value) for value in vector]


#: Экземпляр на процесс. ``None`` — ещё не собран.
_gateway: LlmGateway | None = None


def set_gateway(instance: LlmGateway | None) -> None:
    """Поставить сервис в процесс (сборка сервера) или убрать его (``None``).

    Единственный способ установить чужой сервис — и единственный способ его
    снять. Второй путь (присваивание переменной модуля извне) сделал бы
    состав процесса невидимым и позволил бы оставить после теста сервис,
    собранный на тестовых настройках.
    """
    global _gateway
    _gateway = instance


def gateway() -> LlmGateway:
    """Сервис общения с LLM в этом процессе. Собирается при первом обращении."""
    global _gateway
    if _gateway is None:
        _gateway = LlmGateway()
        logger.info(
            "[llm] %s",
            ", ".join(
                f"{key}={value}"
                for key, value in _gateway.describe().items()
                if key != "embed"
            ),
        )
    return _gateway


def reset_gateway() -> None:
    """Снять сервис процесса. Для тестов: следующий вызов соберёт новый."""
    set_gateway(None)
