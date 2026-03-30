"""
Pydantic 预测输出 Schema。
LLM 必须严格按照此结构返回 JSON，通过 LangChain StructuredOutputParser 强制解析。
"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class MarketPrediction(BaseModel):
    """单个市场的预测基础结构"""
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度 0.0-1.0")

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 3)


class Pred1X2(MarketPrediction):
    """胜平负"""
    prediction: Literal["1", "X", "2"] = Field(..., description="1=主胜 X=平局 2=客胜")


class PredScore(MarketPrediction):
    """比分预测"""
    home: int = Field(..., ge=0, le=20)
    away: int = Field(..., ge=0, le=20)


class PredOU(MarketPrediction):
    """大小球（默认 2.5 球线）"""
    line: float = Field(default=2.5, description="球线，通常为 2.5")
    side: Literal["over", "under"] = Field(..., description="over=大球 under=小球")


class PredAsianHCP(MarketPrediction):
    """亚盘让球"""
    line: str = Field(..., description="让球线，如 '-0.5' / '+1' / '-1.5'")
    side: Literal["home", "away"] = Field(..., description="看好主队还是客队")


class PredBTTS(MarketPrediction):
    """两队都进球"""
    prediction: Literal["yes", "no"]


class MatchPredictionOutput(BaseModel):
    """
    完整的一场比赛预测输出。
    Agent 必须填写所有字段，不确定时 confidence 设为较低值而非留空。
    """
    # 各市场预测
    pred_1x2: Pred1X2
    pred_score: PredScore
    pred_ou_25: PredOU
    pred_asian_hcp: PredAsianHCP
    pred_btts: PredBTTS

    # 综合推荐（Agent 自主决定最有把握的一个市场）
    recommended_market: Literal["1x2", "score", "ou_25", "asian_hcp", "btts"] = Field(
        ..., description="最推荐的投注市场"
    )
    recommended_detail: str = Field(
        ..., description="推荐结果的一句话总结，如 '主队胜，置信度72%'"
    )

    # 分析摘要
    reasoning: str = Field(..., description="综合分析推理过程，200-500字")
    key_factors: list[str] = Field(
        default_factory=list,
        description="影响预测的关键因素列表，3-6条",
    )
    data_quality_note: Optional[str] = Field(
        None, description="数据质量备注，如某源可信度低或数据缺失"
    )


# JSON Schema 字符串，用于注入到 LLM 提示词中
PREDICTION_JSON_SCHEMA = MatchPredictionOutput.model_json_schema()
