"""
全局配置管理，从 .env 文件读取所有配置项。
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    # API Keys
    api_football_key: str = ""
    api_football_host: str = "v3.football.api-sports.io"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # 数据库 & 存储
    db_path: str = "./data/football.db"
    cache_dir: str = "./data/cache"

    # 行为配置
    api_daily_limit: int = 100
    log_level: str = "INFO"
    target_leagues: str = "39,140,78,135,61,169"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("db_path")
    @classmethod
    def validate_db_path(cls, v: str) -> str:
        path = Path(v)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    @field_validator("cache_dir")
    @classmethod
    def validate_cache_dir(cls, v: str) -> str:
        Path(v).mkdir(parents=True, exist_ok=True)
        return v

    @property
    def league_ids(self) -> list[int]:
        return [int(x.strip()) for x in self.target_leagues.split(",") if x.strip()]

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()

# 联赛名称映射
LEAGUE_NAMES: dict[int, str] = {
    39: "英超",
    140: "西甲",
    78: "德甲",
    135: "意甲",
    61: "法甲",
    169: "中超",
}

# 数据源可信度权重（0.0 ~ 1.0）
SOURCE_CREDIBILITY: dict[str, float] = {
    "api-football": 0.95,
    "bbc-sport": 0.85,
    "espn": 0.80,
    "club-official": 0.80,
    "sky-sports": 0.75,
    "oddsportal": 0.75,
    "twitter-verified": 0.55,
    "twitter-general": 0.40,
    "weibo-general": 0.40,
}
