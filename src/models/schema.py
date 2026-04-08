"""
SQLAlchemy ORM 数据模型定义。
涵盖：球队、球员、比赛、事件、预测、实际结果、数据源可信度。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ #
#  球队
# ------------------------------------------------------------------ #
class Team(Base):
    __tablename__ = "teams"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    api_football_id: int = Column(Integer, unique=True, index=True, nullable=False)
    name: str = Column(String(100), nullable=False, index=True)
    name_cn: Optional[str] = Column(String(100))          # 中文名
    code: Optional[str] = Column(String(5))               # "MUN"
    logo_url: Optional[str] = Column(String(500))
    country: Optional[str] = Column(String(100))
    league_id: Optional[int] = Column(Integer, index=True) # 主联赛 ID
    founded: Optional[int] = Column(Integer)
    venue_name: Optional[str] = Column(String(200))

    # 滚动统计（每轮更新）
    avg_goals_for: Optional[float] = Column(Float)
    avg_goals_against: Optional[float] = Column(Float)
    avg_possession: Optional[float] = Column(Float)

    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    players = relationship("Player", back_populates="team", cascade="all, delete-orphan")
    home_matches = relationship("Match", foreign_keys="Match.home_team_id", back_populates="home_team")
    away_matches = relationship("Match", foreign_keys="Match.away_team_id", back_populates="away_team")

    def __repr__(self) -> str:
        return f"<Team {self.name}>"


# ------------------------------------------------------------------ #
#  球员
# ------------------------------------------------------------------ #
class Player(Base):
    __tablename__ = "players"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    api_football_id: int = Column(Integer, unique=True, index=True, nullable=False)
    name: str = Column(String(200), nullable=False, index=True)
    team_id: int = Column(Integer, ForeignKey("teams.id"), index=True)
    team = relationship("Team", back_populates="players")

    position: Optional[str] = Column(String(30))          # Goalkeeper / Defender / …
    age: Optional[int] = Column(Integer)
    nationality: Optional[str] = Column(String(100))

    # 本赛季统计
    appearances: int = Column(Integer, default=0)
    goals: int = Column(Integer, default=0)
    assists: int = Column(Integer, default=0)
    rating: Optional[float] = Column(Float)               # API-Football 评分 0-10

    # 伤情状态
    is_injured: bool = Column(Boolean, default=False)
    injury_type: Optional[str] = Column(String(200))
    injury_reason: Optional[str] = Column(String(200))   # 来源新闻摘要
    expected_return: Optional[datetime] = Column(DateTime)

    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Player {self.name} {'[伤]' if self.is_injured else ''}>"


# ------------------------------------------------------------------ #
#  比赛
# ------------------------------------------------------------------ #
class Match(Base):
    __tablename__ = "matches"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    api_football_id: int = Column(Integer, unique=True, index=True, nullable=False)

    league_id: int = Column(Integer, nullable=False, index=True)
    league_name: Optional[str] = Column(String(100))
    season: int = Column(Integer, nullable=False, index=True)
    round: Optional[str] = Column(String(50))             # "Regular Season - 30"

    home_team_id: int = Column(Integer, ForeignKey("teams.id"), nullable=False)
    away_team_id: int = Column(Integer, ForeignKey("teams.id"), nullable=False)
    home_team = relationship("Team", foreign_keys=[home_team_id], back_populates="home_matches")
    away_team = relationship("Team", foreign_keys=[away_team_id], back_populates="away_matches")

    match_date: datetime = Column(DateTime, nullable=False, index=True)
    venue: Optional[str] = Column(String(200))
    status: str = Column(String(20), default="scheduled")  # scheduled/live/finished/postponed

    # 实际结果（赛后填入）
    home_goals: Optional[int] = Column(Integer)
    away_goals: Optional[int] = Column(Integer)
    result_1x2: Optional[str] = Column(String(1))          # "1" / "X" / "2"

    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    predictions = relationship("Prediction", back_populates="match", cascade="all, delete-orphan")
    statistics = relationship("MatchStatistic", back_populates="match", cascade="all, delete-orphan")
    news_items = relationship("NewsItem", back_populates="match", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_match_league_date", "league_id", "match_date"),
    )

    def __repr__(self) -> str:
        return f"<Match {self.home_team_id} vs {self.away_team_id} @ {self.match_date.date()}>"


# ------------------------------------------------------------------ #
#  比赛统计
# ------------------------------------------------------------------ #
class MatchStatistic(Base):
    __tablename__ = "match_statistics"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    match_id: int = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    match = relationship("Match", back_populates="statistics")

    team_id: int = Column(Integer, ForeignKey("teams.id"), nullable=False)
    team = relationship("Team")

    # 基础统计
    shots_on_goal: Optional[int] = Column(Integer)
    shots_total: Optional[int] = Column(Integer)
    possession: Optional[float] = Column(Float)            # 百分比 0-100
    passes_accuracy: Optional[float] = Column(Float)       # 0-100
    corners: Optional[int] = Column(Integer)
    fouls: Optional[int] = Column(Integer)
    yellow_cards: Optional[int] = Column(Integer)
    red_cards: Optional[int] = Column(Integer)
    offsides: Optional[int] = Column(Integer)

    __table_args__ = (
        UniqueConstraint("match_id", "team_id", name="uq_stat_match_team"),
    )


# ------------------------------------------------------------------ #
#  新闻 / 爬虫数据
# ------------------------------------------------------------------ #
class NewsItem(Base):
    __tablename__ = "news_items"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    match_id: Optional[int] = Column(Integer, ForeignKey("matches.id"), index=True)
    match = relationship("Match", back_populates="news_items")

    source_name: str = Column(String(100), nullable=False)
    credibility_score: float = Column(Float, nullable=False)
    url: Optional[str] = Column(String(1000))
    title: Optional[str] = Column(String(500))
    content: Optional[str] = Column(Text)
    category: str = Column(String(50), default="general")  # injury/tactical/morale/odds/general
    published_at: Optional[datetime] = Column(DateTime)
    scraped_at: datetime = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_news_source_scraped", "source_name", "scraped_at"),
    )


# ------------------------------------------------------------------ #
#  预测
# ------------------------------------------------------------------ #
class Prediction(Base):
    __tablename__ = "predictions"

    id: str = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    match_id: int = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    match = relationship("Match", back_populates="predictions")

    # 各市场预测（JSON 结构，见 output_schemas.py）
    pred_1x2: Optional[dict] = Column(JSON)        # {"prediction":"1","confidence":0.72}
    pred_score: Optional[dict] = Column(JSON)      # {"home":2,"away":1,"confidence":0.45}
    pred_ou_25: Optional[dict] = Column(JSON)      # {"side":"over","confidence":0.68}
    pred_asian_hcp: Optional[dict] = Column(JSON)  # {"line":"-0.5","side":"home","confidence":0.55}
    pred_btts: Optional[dict] = Column(JSON)       # {"prediction":"yes","confidence":0.60}

    # 综合推荐
    recommended_market: Optional[str] = Column(String(20))   # "1x2"/"ou_25"/"asian_hcp"/…
    recommended_detail: Optional[str] = Column(String(500))  # "主胜，置信度72%"

    # 元数据
    prompt_version: int = Column(Integer, default=1)
    reasoning: Optional[str] = Column(Text)        # LLM 推理链
    avg_credibility: Optional[float] = Column(Float)
    sources_used: Optional[list] = Column(JSON)    # 使用的数据源列表
    llm_provider: Optional[str] = Column(String(30))  # "gemini" / "openai"

    created_at: datetime = Column(DateTime, default=datetime.utcnow, index=True)

    actual_result = relationship(
        "ActualResult", back_populates="prediction", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Prediction {self.id[:8]} match={self.match_id}>"


# ------------------------------------------------------------------ #
#  实际结果 & 准确率
# ------------------------------------------------------------------ #
class ActualResult(Base):
    __tablename__ = "actual_results"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id: str = Column(String(36), ForeignKey("predictions.id"), unique=True, nullable=False)
    prediction = relationship("Prediction", back_populates="actual_result")

    # 比赛真实结果
    result_1x2: Optional[str] = Column(String(1))    # "1" / "X" / "2"
    home_goals: Optional[int] = Column(Integer)
    away_goals: Optional[int] = Column(Integer)
    total_goals: Optional[int] = Column(Integer)
    btts: Optional[bool] = Column(Boolean)            # 双方都进球

    # 各市场准确率
    correct_1x2: Optional[bool] = Column(Boolean)
    correct_score: Optional[bool] = Column(Boolean)
    correct_ou_25: Optional[bool] = Column(Boolean)
    correct_asian_hcp: Optional[bool] = Column(Boolean)
    correct_btts: Optional[bool] = Column(Boolean)

    # 综合得分 0.0-1.0（各市场正确率均值）
    overall_accuracy: Optional[float] = Column(Float)

    recorded_at: datetime = Column(DateTime, default=datetime.utcnow)


# ------------------------------------------------------------------ #
#  数据源可信度追踪
# ------------------------------------------------------------------ #
class SourceCredibility(Base):
    __tablename__ = "source_credibility"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    source_name: str = Column(String(100), unique=True, nullable=False, index=True)

    # 基础可信度（初始值来自 config.py SOURCE_CREDIBILITY）
    base_score: float = Column(Float, nullable=False)
    # 动态准确率（根据历史预测贡献自动更新）
    dynamic_score: Optional[float] = Column(Float)

    total_data_points: int = Column(Integer, default=0)
    verified_correct: int = Column(Integer, default=0)   # 数据核实为准确的次数

    notes: Optional[str] = Column(Text)
    last_updated: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def effective_score(self) -> float:
        """返回实际使用的可信度权重（优先动态分，不足50条数据时用基础分）"""
        if self.dynamic_score is not None and self.total_data_points >= 50:
            return self.dynamic_score
        return self.base_score

    def __repr__(self) -> str:
        return f"<SourceCredibility {self.source_name}={self.effective_score:.2f}>"


# ------------------------------------------------------------------ #
#  提示词版本追踪（用于 A/B 测试）
# ------------------------------------------------------------------ #
class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    version: int = Column(Integer, unique=True, nullable=False)
    system_prompt: str = Column(Text, nullable=False)
    description: Optional[str] = Column(String(500))
    is_active: bool = Column(Boolean, default=False)

    # 准确率统计（在此版本下产生的所有预测的平均值）
    predictions_count: int = Column(Integer, default=0)
    accuracy_1x2: Optional[float] = Column(Float)
    accuracy_ou_25: Optional[float] = Column(Float)
    accuracy_asian_hcp: Optional[float] = Column(Float)

    created_at: datetime = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<PromptVersion v{self.version} active={self.is_active}>"
