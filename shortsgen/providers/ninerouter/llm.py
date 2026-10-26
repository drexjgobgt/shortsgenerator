"""Adapter LLM -> 9Router /v1/chat/completions, dengan rantai fallback model."""
from __future__ import annotations

from typing import Optional, Sequence

from ...errors import ProviderError
from ..base import ChatMessage, HealthStatus, LLMProvider, LLMResult, ProviderHealth
from .client import NineRouterClient

DEFAULT_TINY_PROBE_PROMPT = "Balas hanya dengan kata: OK"


class LLMAdapter:
    """LLM via 9Router dengan fallback model berurutan.

    Urutan percobaan: model yang diminta (atau primary) -> daftar fallback.
    Fallback hanya dilakukan untuk error yang retryable (429/5xx/jaringan).
    """

    name = "9router"

    def __init__(
        self,
        client: NineRouterClient,
        primary_model: str,
        fallback_models: Sequence[str] = (),
        *,
        default_temperature: float = 0.7,
        default_max_tokens: Optional[int] = None,
        logger: Optional[object] = None,
        cache: Optional[object] = None,
    ):
        self.client = client
        self.primary_model = primary_model
        self.fallback_models = list(fallback_models)
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.logger = logger
        self.cache = cache

    @property
    def configured_models(self) -> list[str]:
        models = [self.primary_model] if self.primary_model else []
        models += [m for m in self.fallback_models if m and m not in models]
        return models

    def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        candidates = [model] if model else list(self.configured_models)
        if model:
            candidates += [m for m in self.configured_models if m != model]
        if not candidates:
            from ...errors import NotConfiguredError

            raise NotConfiguredError(self.name, "llm", "Tidak ada LLM_MODEL yang dikonfigurasi")

        errors: list[str] = []
        for candidate in candidates:
            cache_key = {
                "messages": messages,
                "model": candidate,
                "temperature": temperature,
                "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            }
            if self.cache is not None:
                cached = self.cache.get_json(cache_key)
                if cached and cached.get("text"):
                    return LLMResult(
                        text=str(cached["text"]),
                        model=str(cached.get("model") or candidate),
                        provider=self.name,
                        usage={**(cached.get("usage") or {}), "cached": True},
                        transport="cache",
                        latency_ms=0,
                        raw=cached.get("raw"),
                    )
            try:
                response = self.client.chat(
                    messages,
                    candidate,
                    temperature=temperature,
                    max_tokens=max_tokens if max_tokens is not None else self.default_max_tokens,
                    # satu percobaan per model: rantai fallback lebih cepat daripada retry lama
                    attempts=1,
                )
                if not response.text:
                    raise ProviderError(
                        self.name,
                        "respons chat kosong",
                        kind="empty_response",
                        retryable=True,
                        model=candidate,
                    )
                result = LLMResult(
                    text=response.text,
                    model=response.model or candidate,
                    provider=self.name,
                    usage=response.usage,
                    transport=response.transport,
                    latency_ms=response.latency_ms,
                    raw=response.raw,
                )
                if self.cache is not None:
                    self.cache.set_json(
                        cache_key,
                        {
                            "text": result.text,
                            "model": result.model,
                            "usage": result.usage,
                            "raw": result.raw,
                        },
                    )
                return result
            except ProviderError as exc:
                detail = str(exc)
                errors.append(f"{candidate}: {detail}")
                if exc.retryable and candidate != candidates[-1]:
                    self._log(
                        f"model {candidate} gagal (retryable: {exc.kind}"
                        + (f", HTTP {exc.status}" if exc.status else "")
                        + "), mencoba fallback berikutnya"
                    )
                    continue
                if len(errors) > 1:
                    # Beberapa model sudah dicoba: laporkan seluruh rantai agar
                    # akar masalah (mis. 524 pada satu provider) terlihat jelas.
                    raise ProviderError(
                        self.name,
                        "semua model gagal: " + " | ".join(errors),
                        kind="all_failed",
                        retryable=True,
                    ) from exc
                raise
        raise ProviderError(self.name, "semua model gagal: " + " | ".join(errors), kind="all_failed")

    def health(self) -> ProviderHealth:
        if not self.configured_models:
            return ProviderHealth(
                provider=self.name,
                capability="llm",
                status=HealthStatus.NOT_CONFIGURED,
                detail="LLM_MODEL kosong",
            )
        return ProviderHealth(
            provider=self.name,
            capability="llm",
            status=HealthStatus.AVAILABLE,
            detail=f"model: {', '.join(self.configured_models[:4])}"
            + (" ..." if len(self.configured_models) > 4 else ""),
            meta={"models": self.configured_models},
        )

    def probe(self) -> ProviderHealth:
        """Probe nyata berbiaya minimal (1 panggilan kecil) untuk `doctor --deep`."""
        base = self.health()
        if base.status != HealthStatus.AVAILABLE:
            return base
        try:
            result = self.chat(
                [{"role": "user", "content": DEFAULT_TINY_PROBE_PROMPT}],
                temperature=0.0,
                max_tokens=8,
            )
            return ProviderHealth(
                provider=self.name,
                capability="llm",
                status=HealthStatus.AVAILABLE,
                detail=f"probe ok via {result.model} ({result.transport})",
                latency_ms=result.latency_ms,
                meta={"usage": result.usage},
            )
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name,
                capability="llm",
                status=HealthStatus.ERROR,
                detail=f"probe gagal: {exc}",
            )

    def _log(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "warning"):
            self.logger.warning(message)
