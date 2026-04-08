"""
LLM 提供者封装：仅使用 Gemini 2.5 Flash。
免费层限制：10 RPM（每分钟10次）/ 500 RPD（每天500次）/ 100万 TPM。
503 过载时自动指数退避重试，超出后明确告知用户原因。
"""
import json
import time
import warnings
from typing import Any, Optional

warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality", category=UserWarning)
warnings.filterwarnings("ignore", module=r"pydantic\.v1", category=UserWarning)

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from src.config import settings
from src.logger import get_logger
from src.prediction.output_schemas import MatchPredictionOutput

logger = get_logger(__name__)

# Gemini 免费层使用限制（用于错误提示）
_RATE_LIMIT_INFO = "gemini-2.5-flash 免费层限制：10次/分钟、500次/天。请稍后再试，或升级至付费计划。"


def _is_server_overload(exc: BaseException) -> bool:
    """503/过载错误值得重试；404/401/403 是配置问题，不重试。"""
    msg = str(exc)
    if "404" in msg or "NOT_FOUND" in msg or "401" in msg or "403" in msg:
        return False
    return "503" in msg or "UNAVAILABLE" in msg or "high demand" in msg or "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _build_gemini() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.3,
        max_retries=0,  # 由 tenacity 控制，避免双重重试
    )


def search_with_gemini(query: str, retries: int = 5) -> str:
    """
    使用 gemini-2.5-flash + Google Search Grounding 搜索实时信息。

    503 UNAVAILABLE = 服务器瞬时容量不足（非速率限制），自动指数退避重试。
    重试间隔：15s → 30s → 60s → 120s → 放弃
    """
    import random
    from google import genai
    from google.genai import types as _gtypes

    client = genai.Client(api_key=settings.gemini_api_key)
    last_exc: Optional[Exception] = None

    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=query,
                config=_gtypes.GenerateContentConfig(
                    tools=[_gtypes.Tool(google_search=_gtypes.GoogleSearch())],
                    temperature=0.1,
                ),
            )
            return response.text or ""
        except Exception as exc:
            last_exc = exc
            if _is_server_overload(exc) and attempt < retries - 1:
                base_wait = 15 * (2 ** attempt)  # 15s → 30s → 60s → 120s
                wait = base_wait + random.uniform(0, 5)
                logger.warning(
                    "Gemini Search 503 服务器过载（第 %d/%d 次），%.0fs 后重试...",
                    attempt + 1, retries, wait,
                )
                time.sleep(wait)
            else:
                raise
    raise last_exc  # type: ignore[misc]


class LLMProvider:
    """仅使用 gemini-2.5-flash，503 过载时重试，超限后给出清晰提示。"""

    def __init__(self) -> None:
        self._gemini: Optional[ChatGoogleGenerativeAI] = None

    def _get_gemini(self) -> ChatGoogleGenerativeAI:
        if self._gemini is None:
            self._gemini = _build_gemini()
        return self._gemini

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(min=10, max=60),
        retry=retry_if_exception(_is_server_overload),
        reraise=True,
    )
    def _call_gemini(self, messages: list) -> tuple[str, str]:
        """调用 gemini-2.5-flash，503 过载时指数退避重试最多 4 次（10s/20s/40s/60s）。"""
        response = self._get_gemini().invoke(messages)
        return response.content, "gemini-2.5-flash"

    def call(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        """
        调用 gemini-2.5-flash，返回 (raw_text, provider_name)。
        若持续503，说明当前分钟内 API 调用次数已达上限（10 RPM）。
        """
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        try:
            return self._call_gemini(messages)
        except Exception as exc:
            if _is_server_overload(exc):
                raise RuntimeError(
                    f"gemini-2.5-flash 持续过载，重试4次后仍失败。\n"
                    f"原因：{_RATE_LIMIT_INFO}\n"
                    f"建议：等待1分钟后重新运行，或减少同时预测的比赛数量。"
                ) from exc
            raise RuntimeError(
                f"gemini-2.5-flash 调用失败（非过载错误）: {exc}"
            ) from exc

    def call_structured(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[MatchPredictionOutput, str]:
        """调用 LLM 并解析为 MatchPredictionOutput，返回 (parsed_prediction, provider_name)。"""
        raw, provider = self.call(system_prompt, user_prompt)
        parsed = _extract_and_parse_json(raw)
        return MatchPredictionOutput(**parsed), provider


def _extract_and_parse_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 块（容忍 markdown 代码块包裹）"""
    text = text.strip()
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        if start != end:
            text = text[start + 3: end].strip()
            if text.startswith("json"):
                text = text[4:].strip()
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start == -1 or brace_end == -1:
        raise ValueError(f"LLM 输出中未找到有效 JSON: {text[:200]}")
    return json.loads(text[brace_start: brace_end + 1])


# 全局单例
llm_provider = LLMProvider()

