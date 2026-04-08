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
    # 英超 (短名 / football-data.org 全名)
    "Manchester City": "曼城", "Manchester City FC": "曼城",
    "Manchester United": "曼联", "Manchester United FC": "曼联",
    "Liverpool": "利物浦", "Liverpool FC": "利物浦",
    "Arsenal": "阿森纳", "Arsenal FC": "阿森纳",
    "Chelsea": "切尔西", "Chelsea FC": "切尔西",
    "Tottenham": "热刺", "Tottenham Hotspur FC": "热刺",
    "Newcastle": "纽卡斯尔", "Newcastle United FC": "纽卡斯尔",
    "Aston Villa": "阿斯顿维拉", "Aston Villa FC": "阿斯顿维拉",
    "West Ham United FC": "西汉姆", "West Ham": "西汉姆",
    "Brentford FC": "布伦特福德",
    "Brighton & Hove Albion FC": "布莱顿", "Brighton": "布莱顿",
    "Crystal Palace FC": "水晶宫", "Crystal Palace": "水晶宫",
    "Everton FC": "埃弗顿", "Everton": "埃弗顿",
    "Burnley FC": "伯恩利", "Burnley": "伯恩利",
    "Wolverhampton Wanderers FC": "狼队", "Wolverhampton": "狼队",
    "Nottingham Forest FC": "诺丁汉森林",
    "Fulham FC": "富勒姆",
    "Sunderland AFC": "桑德兰",
    "AFC Bournemouth": "伯恩茅斯",
    # 西甲 (短名 / football-data.org 全名)
    "Real Madrid": "皇马", "Real Madrid CF": "皇马",
    "Barcelona": "巴萨", "FC Barcelona": "巴萨",
    "Atletico Madrid": "马竞", "Club Atlético de Madrid": "马竞",
    "Sevilla": "塞维利亚", "Sevilla FC": "塞维利亚",
    "Athletic Club": "毕尔巴鄂",
    "Villarreal CF": "比利亚雷亚尔",
    "Real Sociedad de Fútbol": "皇家社会",
    "Girona FC": "赫罗纳",
    "Valencia CF": "瓦伦西亚", "Valencia": "瓦伦西亚",
    "RC Celta de Vigo": "塞尔塔",
    "Deportivo Alavés": "阿拉维斯",
    "CA Osasuna": "奥萨苏纳",
    "Real Betis Balompié": "皇家贝蒂斯",
    "RCD Mallorca": "马洛卡",
    "Rayo Vallecano de Madrid": "拉约瓦列卡诺",
    "RCD Espanyol de Barcelona": "西班牙人",
    "Getafe CF": "赫塔费",
    "Elche CF": "埃尔切",
    "Real Oviedo": "奥维耶多",
    # 德甲 (短名 / football-data.org 全名)
    "Bayern Munich": "拜仁", "FC Bayern München": "拜仁",
    "Borussia Dortmund": "多特",
    "RB Leipzig": "莱比锡",
    "Bayer Leverkusen": "勒沃库森", "Bayer 04 Leverkusen": "勒沃库森",
    "Eintracht Frankfurt": "法兰克福",
    "VfB Stuttgart": "斯图加特",
    "SC Freiburg": "弗赖堡",
    "1. FSV Mainz 05": "美因茨",
    "VfL Wolfsburg": "沃尔夫斯堡",
    "1. FC Union Berlin": "柏林联合",
    "FC St. Pauli 1910": "圣保利",
    "Borussia Mönchengladbach": "门兴",
    "1. FC Köln": "科隆",
    "FC Augsburg": "奥格斯堡",
    "TSG 1899 Hoffenheim": "霍芬海姆",
    "SV Werder Bremen": "不来梅",
    "1. FC Heidenheim 1846": "海登海姆",
    "Hamburger SV": "汉堡",
    # 意甲 (短名 / football-data.org 全名)
    "Inter": "国际米兰", "FC Internazionale Milano": "国际米兰",
    "AC Milan": "AC米兰",
    "Juventus": "尤文图斯", "Juventus FC": "尤文图斯",
    "Napoli": "那不勒斯", "SSC Napoli": "那不勒斯",
    "Atalanta BC": "亚特兰大", "Atalanta": "亚特兰大",
    "AS Roma": "罗马",
    "Bologna FC 1909": "博洛尼亚",
    "Udinese Calcio": "乌迪内斯",
    "Torino FC": "都灵",
    "US Lecce": "莱切",
    "Genoa CFC": "热那亚",
    "US Cremonese": "克雷莫内塞",
    "AC Pisa 1909": "比萨",
    "Como 1907": "科莫",
    "Parma Calcio 1913": "帕尔马",
    "US Sassuolo Calcio": "萨苏奥洛",
    "Cagliari Calcio": "卡利亚里",
    "Hellas Verona FC": "维罗纳",
    # 法甲 (短名 / football-data.org 全名)
    "Paris Saint-Germain": "巴黎圣日耳曼", "Paris Saint-Germain FC": "巴黎圣日耳曼",
    "Marseille": "马赛", "Olympique de Marseille": "马赛",
    "Monaco": "摩纳哥", "AS Monaco FC": "摩纳哥",
    "Lyon": "里昂", "Olympique Lyonnais": "里昂",
    "Lille OSC": "里尔",
    "OGC Nice": "尼斯",
    "Stade Rennais FC 1901": "雷恩",
    "AJ Auxerre": "欧塞尔",
    "Toulouse FC": "图卢兹",
    "Paris FC": "巴黎FC",
    "Angers SCO": "昂热",
    "Le Havre AC": "勒阿弗尔",
    "FC Metz": "梅斯",
    "FC Lorient": "洛里昂",
    "FC Nantes": "南特",
    # 欧冠特有球队
    "Sporting Clube de Portugal": "体育里斯本",
}
