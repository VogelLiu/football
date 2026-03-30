"""
API-Football 客户端，包含：
- 每日请求计数器（防止超出免费层限额）
- 自动缓存（减少不必要的配额消耗）
- 主要数据拉取方法
"""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

_USAGE_FILE = Path(settings.cache_dir) / "api_usage.json"
_BASE_URL = f"https://{settings.api_football_host}"
_HEADERS = {
    "x-rapidapi-key": settings.api_football_key,
    "x-rapidapi-host": settings.api_football_host,
}


# ------------------------------------------------------------------ #
#  每日请求计数器
# ------------------------------------------------------------------ #
class DailyLimitExceeded(Exception):
    pass


def _load_usage() -> dict:
    if _USAGE_FILE.exists():
        try:
            data = json.loads(_USAGE_FILE.read_text())
            if data.get("date") == str(date.today()):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"date": str(date.today()), "count": 0}


def _save_usage(data: dict) -> None:
    _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USAGE_FILE.write_text(json.dumps(data))


def _check_and_increment() -> int:
    """检查今日配额，并递增计数器。返回本次用后的累计数。"""
    usage = _load_usage()
    if usage["count"] >= settings.api_daily_limit:
        raise DailyLimitExceeded(
            f"今日 API 请求已达上限 {settings.api_daily_limit} 次，明日 UTC 00:00 後重置。\n"
            "如需升级，请访问 https://www.api-football.com/"
        )
    usage["count"] += 1
    _save_usage(usage)
    remaining = settings.api_daily_limit - usage["count"]
    logger.debug("API 配额使用: %d/%d（剩余 %d）", usage["count"], settings.api_daily_limit, remaining)
    return usage["count"]


def get_remaining_quota() -> int:
    usage = _load_usage()
    return max(0, settings.api_daily_limit - usage["count"])


# ------------------------------------------------------------------ #
#  缓存层
# ------------------------------------------------------------------ #
_CACHE_TTL: dict[str, int] = {
    "fixtures": 24 * 3600,
    "standings": 12 * 3600,
    "injuries": 6 * 3600,
    "team_statistics": 12 * 3600,
    "head_to_head": 7 * 24 * 3600,
    "players": 24 * 3600,
    "results": 3600,
}


def _cache_path(endpoint: str, params: dict) -> Path:
    key = endpoint.replace("/", "_") + "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
    return Path(settings.cache_dir) / f"{key}.json"


def _read_cache(path: Path, ttl: int) -> Optional[Any]:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl:
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _write_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


# ------------------------------------------------------------------ #
#  底层请求
# ------------------------------------------------------------------ #
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _request(endpoint: str, params: dict, cache_category: str) -> dict:
    """发送 API 请求，优先返回缓存数据。"""
    ttl = _CACHE_TTL.get(cache_category, 3600)
    cache_file = _cache_path(endpoint, params)
    cached = _read_cache(cache_file, ttl)
    if cached is not None:
        logger.debug("命中缓存: %s %s", endpoint, params)
        return cached

    _check_and_increment()
    url = f"{_BASE_URL}/{endpoint}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()

    if data.get("errors"):
        logger.warning("API 返回错误: %s", data["errors"])

    _write_cache(cache_file, data)
    time.sleep(6)  # 10次/分钟限速 → 间隔 6 秒
    return data


# ------------------------------------------------------------------ #
#  公开 API 方法
# ------------------------------------------------------------------ #
def get_fixtures(league_id: int, season: int, next_n: int = 10) -> list[dict]:
    """获取指定联赛下一批 upcoming 赛程"""
    data = _request("fixtures", {"league": league_id, "season": season, "next": next_n}, "fixtures")
    return data.get("response", [])


def get_fixture_detail(fixture_id: int) -> dict:
    """获取单场比赛完整数据（含统计、事件、阵容）"""
    data = _request("fixtures", {"id": fixture_id}, "fixtures")
    results = data.get("response", [])
    return results[0] if results else {}


def get_standings(league_id: int, season: int) -> list[dict]:
    """获取联赛积分榜"""
    data = _request("standings", {"league": league_id, "season": season}, "standings")
    try:
        return data["response"][0]["league"]["standings"][0]
    except (IndexError, KeyError):
        return []


def get_team_statistics(team_id: int, league_id: int, season: int) -> dict:
    """获取球队本赛季统计"""
    data = _request(
        "teams/statistics",
        {"team": team_id, "league": league_id, "season": season},
        "team_statistics",
    )
    return data.get("response", {})


def get_injuries(fixture_id: int) -> list[dict]:
    """获取某场比赛的伤兵名单"""
    data = _request("injuries", {"fixture": fixture_id}, "injuries")
    return data.get("response", [])


def get_head_to_head(team1_id: int, team2_id: int, last: int = 10) -> list[dict]:
    """获取两队历史对阵"""
    data = _request(
        "fixtures/headtohead",
        {"h2h": f"{team1_id}-{team2_id}", "last": last},
        "head_to_head",
    )
    return data.get("response", [])


def get_team_form(team_id: int, last: int = 5) -> list[dict]:
    """获取球队最近 N 场比赛结果"""
    data = _request("fixtures", {"team": team_id, "last": last}, "fixtures")
    return data.get("response", [])


def get_players(team_id: int, season: int) -> list[dict]:
    """获取球队球员名单及统计"""
    data = _request("players", {"team": team_id, "season": season}, "players")
    return data.get("response", [])


def get_finished_results(league_id: int, season: int, last: int = 5) -> list[dict]:
    """获取联赛最近 N 场已完成比赛（用于结果回填）"""
    data = _request(
        "fixtures",
        {"league": league_id, "season": season, "last": last, "status": "FT"},
        "results",
    )
    return data.get("response", [])
