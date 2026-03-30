"""
数据源定义：联赛覆盖范围、球队名称映射、爬虫启用配置。
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LeagueConfig:
    league_id: int
    name: str
    name_cn: str
    season: int
    country: str
    # Sky Sports URL slug（用于爬虫）
    sky_sports_slug: Optional[str] = None


@dataclass
class SourceConfig:
    name: str           # 唯一标识，对应 SOURCE_CREDIBILITY key
    enabled: bool
    description: str
    scrape_interval_hours: int = 6


# ------------------------------------------------------------------ #
#  目标联赛配置
# ------------------------------------------------------------------ #
LEAGUES: list[LeagueConfig] = [
    LeagueConfig(39,  "Premier League",  "英超",  2025, "England",  "premier-league"),
    LeagueConfig(140, "La Liga",          "西甲",  2025, "Spain",    "la-liga"),
    LeagueConfig(78,  "Bundesliga",       "德甲",  2025, "Germany",  "bundesliga"),
    LeagueConfig(135, "Serie A",          "意甲",  2025, "Italy",    "serie-a"),
    LeagueConfig(61,  "Ligue 1",          "法甲",  2025, "France",   "ligue-1"),
    LeagueConfig(169, "Chinese Super League", "中超", 2026, "China", None),
]

LEAGUE_BY_ID: dict[int, LeagueConfig] = {lg.league_id: lg for lg in LEAGUES}


# ------------------------------------------------------------------ #
#  数据源配置
# ------------------------------------------------------------------ #
SOURCES: list[SourceConfig] = [
    SourceConfig("api-football",    True,  "官方结构化数据（赛程/伤兵/统计/对阵）",       24),
    SourceConfig("bbc-sport",       True,  "BBC体育新闻（伤兵/赛前分析）",                 12),
    SourceConfig("sky-sports",      True,  "Sky Sports（赔率预览/战术新闻）",              12),
    SourceConfig("oddsportal",      True,  "OddsPortal（博彩赔率聚合）",                   48),
    SourceConfig("club-official",   False, "官方俱乐部网站（出场阵容）",                   24),
    SourceConfig("twitter-verified",False, "Twitter/X 认证账号（实时伤兵）",               2),
]

SOURCE_BY_NAME: dict[str, SourceConfig] = {s.name: s for s in SOURCES}


# ------------------------------------------------------------------ #
#  球队英文名 → 中文名映射（常见球队）
# ------------------------------------------------------------------ #
TEAM_NAME_CN: dict[str, str] = {
    # 英超
    "Manchester City": "曼城", "Manchester United": "曼联",
    "Liverpool": "利物浦", "Arsenal": "阿森纳",
    "Chelsea": "切尔西", "Tottenham": "热刺",
    "Newcastle": "纽卡斯尔", "Aston Villa": "阿斯顿维拉",
    # 西甲
    "Real Madrid": "皇马", "Barcelona": "巴萨",
    "Atletico Madrid": "马竞", "Sevilla": "塞维利亚",
    # 德甲
    "Bayern Munich": "拜仁", "Borussia Dortmund": "多特",
    "RB Leipzig": "莱比锡", "Bayer Leverkusen": "勒沃库森",
    # 意甲
    "Inter": "国际米兰", "AC Milan": "AC米兰",
    "Juventus": "尤文图斯", "Napoli": "那不勒斯",
    # 法甲
    "Paris Saint-Germain": "巴黎圣日耳曼", "Marseille": "马赛",
    "Monaco": "摩纳哥", "Lyon": "里昂",
}
