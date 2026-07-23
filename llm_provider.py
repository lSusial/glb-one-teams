"""
LLM 프로바이더 추상화.

목적: 작업별로 프로바이더/모델을 갈아끼울 수 있게 한다("AI 프로바이더 실험").
  - role="fast"  → 저비용·고속 모델 (llm_prefilter)
  - role="smart" → 분석·합성 모델 (llm_ranker, briefing)

지원 어댑터:
  - AnthropicProvider : 실제 호출 (anthropic SDK 필요, ANTHROPIC_API_KEY)
  - OpenAIProvider    : 스캐폴드 (openai SDK 필요) — 확장용
  - StubProvider      : 오프라인 결정론적 (키 불필요, 개발·테스트용)

환경변수:
  LLM_PROVIDER       = anthropic | openai | stub   (기본: config.DEFAULT_PROVIDER)
  ANTHROPIC_API_KEY  = sk-ant-...
  OPENAI_API_KEY     = sk-...

주의: 본 모듈은 정의만 한다. 실제 호출은 호출 측(llm_*.py)에서 일어나며,
      API 키가 없으면 RuntimeError 를 던진다(자동 실행 안전장치).
"""
from __future__ import annotations

import abc
import json
import logging
import os
import re
import time

import config

log = logging.getLogger("llm_provider")


# ---------------------------------------------------------------------------
# 추상 인터페이스
# ---------------------------------------------------------------------------
class LLMProvider(abc.ABC):
    #: ai_model 컬럼 기록용 식별자 (예: "anthropic:claude-sonnet-4-6")
    model_id: str = "unknown"

    @abc.abstractmethod
    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, temperature: float = 0.0
    ) -> str:
        """system/user 프롬프트 → 모델 텍스트 응답."""

    def complete_json(self, system: str, user: str, **kw) -> dict:
        """JSON 출력 태스크용. 응답에서 첫 JSON 객체를 추출해 dict 반환(실패 시 {})."""
        raw = self.complete(system, user, **kw)
        return _extract_json(raw)

    # -- 일괄 처리 ---------------------------------------------------------
    # requests: list[(custom_id, system, user, max_tokens)]  →  {custom_id: text}
    # 기본 구현은 동기 순차 호출(폴백). 배치를 지원하는 어댑터는 오버라이드한다.
    def complete_batch(self, requests, *, temperature: float = 0.0) -> dict:
        out = {}
        for cid, system, user, mt in requests:
            out[cid] = self.complete(system, user, max_tokens=mt, temperature=temperature)
        return out

    def complete_json_batch(self, requests, *, temperature: float = 0.0) -> dict:
        """requests → {custom_id: parsed_dict}. 배치 어댑터가 있으면 50% 할인 경로 사용."""
        return {cid: _extract_json(txt)
                for cid, txt in self.complete_batch(requests, temperature=temperature).items()}


# ---------------------------------------------------------------------------
# Anthropic (실제 호출)
# ---------------------------------------------------------------------------
class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None, use_batch: bool | None = None):
        try:
            import anthropic  # 지연 임포트 — 미설치 환경 보호
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "anthropic 패키지가 필요합니다. `pip install anthropic` 후 사용하세요."
            ) from e

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY 환경변수가 없습니다. 키 설정 후 실행하세요."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.model_id = f"anthropic:{model}"
        self.use_batch = config.LLM_USE_BATCH if use_batch is None else use_batch

    def complete(self, system, user, *, max_tokens=1024, temperature=0.0) -> str:
        last_err: Exception | None = None
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            try:
                msg = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return "".join(
                    b.text for b in msg.content if getattr(b, "type", "") == "text"
                )
            except Exception as e:  # noqa: BLE001 — SDK 예외 계층 광범위
                last_err = e
                if attempt < config.LLM_MAX_RETRIES:
                    wait = config.LLM_RETRY_BASE_SEC * (attempt + 1)
                    log.warning("Anthropic 재시도 %d/%d (%.1fs): %s",
                                attempt + 1, config.LLM_MAX_RETRIES, wait, e)
                    time.sleep(wait)
        raise RuntimeError(f"Anthropic 호출 실패: {last_err!r}")

    def complete_batch(self, requests, *, temperature=0.0) -> dict:
        """Message Batches API — 토큰 50% 할인. 비실시간 일괄 처리.

        요청을 한 번에 제출하고 완료까지 폴링 후 custom_id 로 결과를 수거한다.
        use_batch=False 이거나 요청 수가 LLM_BATCH_MIN 미만이면 동기 순차 호출로 폴백.
        """
        requests = list(requests)
        if not self.use_batch or len(requests) < config.LLM_BATCH_MIN:
            return super().complete_batch(requests, temperature=temperature)

        out: dict[str, str] = {}
        for chunk in _chunks(requests, config.LLM_BATCH_CHUNK):
            batch_reqs = [
                {
                    "custom_id": cid,
                    "params": {
                        "model": self.model,
                        "max_tokens": mt,
                        "temperature": temperature,
                        "system": system,
                        "messages": [{"role": "user", "content": user}],
                    },
                }
                for cid, system, user, mt in chunk
            ]
            batch = self._client.messages.batches.create(requests=batch_reqs)
            log.info("배치 제출 — id=%s  요청=%d  (24h 내 완료, 50%% 할인)", batch.id, len(chunk))

            waited = 0
            while True:
                b = self._client.messages.batches.retrieve(batch.id)
                if b.processing_status == "ended":
                    break
                if waited >= config.LLM_BATCH_MAX_WAIT_SEC:
                    raise RuntimeError(f"배치 {batch.id} 대기 초과({waited}s)")
                time.sleep(config.LLM_BATCH_POLL_SEC)
                waited += config.LLM_BATCH_POLL_SEC
                counts = getattr(b, "request_counts", None)
                if counts is not None:
                    log.info("배치 진행 — 처리중=%s 성공=%s 실패=%s (%ds 경과)",
                             getattr(counts, "processing", "?"), getattr(counts, "succeeded", "?"),
                             getattr(counts, "errored", "?"), waited)

            errs = 0
            for res in self._client.messages.batches.results(batch.id):
                r = res.result
                if getattr(r, "type", "") == "succeeded":
                    msg = r.message
                    out[res.custom_id] = "".join(
                        blk.text for blk in msg.content if getattr(blk, "type", "") == "text"
                    )
                else:
                    errs += 1
                    out[res.custom_id] = ""
            log.info("배치 수거 — id=%s  성공=%d  실패=%d", batch.id, len(chunk) - errs, errs)
        return out


# ---------------------------------------------------------------------------
# OpenAI (스캐폴드 — 확장용)
# ---------------------------------------------------------------------------
class OpenAIProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "openai 패키지가 필요합니다. `pip install openai` 후 사용하세요."
            ) from e
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY 환경변수가 없습니다.")
        self._client = openai.OpenAI(api_key=key)
        self.model = model
        self.model_id = f"openai:{model}"

    def complete(self, system, user, *, max_tokens=1024, temperature=0.0) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Stub (오프라인 — 키 불필요)
# ---------------------------------------------------------------------------
class StubProvider(LLMProvider):
    """실제 호출 없이 빈 JSON을 반환. 호출 측은 규칙 기반 폴백으로 동작한다."""

    def __init__(self, role: str = "smart"):
        self.model_id = f"stub:{role}"

    def complete(self, system, user, *, max_tokens=1024, temperature=0.0) -> str:
        return "{}"


# ---------------------------------------------------------------------------
# 팩토리
# ---------------------------------------------------------------------------
def get_provider(role: str = "smart", name: str | None = None,
                 use_batch: bool | None = None) -> LLMProvider:
    """role: 'fast'|'smart'. name 미지정 시 config.DEFAULT_PROVIDER 사용.

    use_batch: Anthropic 전용. None=config 기본(True), False=동기 호출(디버깅).
    """
    name = (name or config.DEFAULT_PROVIDER).lower()
    if name == "anthropic":
        model = config.ANTHROPIC_MODEL_FAST if role == "fast" else config.ANTHROPIC_MODEL_SMART
        return AnthropicProvider(model=model, use_batch=use_batch)
    if name == "openai":
        model = config.OPENAI_MODEL_FAST if role == "fast" else config.OPENAI_MODEL_SMART
        return OpenAIProvider(model=model)
    if name == "stub":
        return StubProvider(role=role)
    raise ValueError(f"알 수 없는 LLM 프로바이더: {name!r} (anthropic|openai|stub)")


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _chunks(seq, n):
    """seq 를 최대 n 개씩 끊어 반환."""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ---------------------------------------------------------------------------
# JSON 추출 유틸
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """모델 응답에서 첫 JSON 객체를 best-effort 로 파싱."""
    if not text:
        return {}
    text = text.strip()
    # 코드펜스 제거
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 본문 중 첫 {...} 블록 시도
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}
