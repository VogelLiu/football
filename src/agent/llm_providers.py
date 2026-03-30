"""
LLM 提供者封装：Gemini 2.0 Flash（主）+ OpenAI GPT-4o（备用）。
提供统一的 call_llm() 接口，自动故障切换。
"""
import json
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.logger import get_logger
from src.prediction.output_schemas import MatchPredictionOutput

logger = get_logger(__name__)


def _build_gemini() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.3,
        max_retries=2,
    )


def _build_openai() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0.3,
        max_retries=2,
    )


class LLMProvider:
    """统一 LLM 调用入口，支持 Gemini → OpenAI 自动故障切换"""

    def __init__(self) -> None:
        self._gemini: Optional[ChatGoogleGenerativeAI] = None
        self._openai: Optional[ChatOpenAI] = None

    def _get_gemini(self) -> ChatGoogleGenerativeAI:
        if self._gemini is None:
            self._gemini = _build_gemini()
        return self._gemini

    def _get_openai(self) -> ChatOpenAI:
        if self._openai is None:
            self._openai = _build_openai()
        return self._openai

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=8))
    def _call_gemini(self, messages: list) -> tuple[str, str]:
        """返回 (content, provider_name)"""
        response = self._get_gemini().invoke(messages)
        return response.content, "gemini"

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=8))
    def _call_openai(self, messages: list) -> tuple[str, str]:
        response = self._get_openai().invoke(messages)
        return response.content, "openai"

    def call(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        """
        调用 LLM，返回 (raw_text_response, provider_name)。
        优先 Gemini，失败后自动切换 OpenAI。
        """
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        try:
            return self._call_gemini(messages)
        except Exception as e:
            logger.warning("Gemini 调用失败: %s，切换至 OpenAI...", e)
            try:
                return self._call_openai(messages)
            except Exception as e2:
                raise RuntimeError(f"所有 LLM 均调用失败。Gemini: {e}; OpenAI: {e2}") from e2

    def call_structured(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[MatchPredictionOutput, str]:
        """
        调用 LLM 并解析为 MatchPredictionOutput Pydantic 对象。
        返回 (parsed_prediction, provider_name)
        """
        raw, provider = self.call(system_prompt, user_prompt)
        parsed = _extract_and_parse_json(raw)
        return MatchPredictionOutput(**parsed), provider


def _extract_and_parse_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 块（容忍 markdown 代码块包裹）"""
    text = text.strip()

    # 去掉 ```json ... ``` 包裹
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        if start != end:
            text = text[start + 3: end].strip()
            if text.startswith("json"):
                text = text[4:].strip()

    # 找到第一个 { 和最后一个 }
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start == -1 or brace_end == -1:
        raise ValueError(f"LLM 输出中未找到有效 JSON: {text[:200]}")

    json_str = text[brace_start: brace_end + 1]
    return json.loads(json_str)


# 全局单例
llm_provider = LLMProvider()
