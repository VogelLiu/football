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
    """获取指定联赛下一批 upcoming 赛程（需要付费计划的 next 参数）"""
    data = _request("fixtures", {"league": league_id, "season": season, "next": next_n}, "fixtures")
    return data.get("response", [])


def get_fixtures_by_date(league_id: int, season: int, date: str) -> list[dict]:
    """
    获取指定联赛在某日期的赛程（免费计划可用）。
    date 格式: 'YYYY-MM-DD'
    """
    data = _request(
        "fixtures",
        {"league": league_id, "season": season, "date": date},
        "fixtures",
    )
    return data.get("response", [])


def search_fixtures_around_date(league_id: int, season: int, days_window: int = 3) -> list[dict]:
    """
    搜索当前日期前后 days_window 天内的赛程（逐日查询，免费计划兼容）。
    合并结果去重后返回。
    """
    from datetime import date as date_cls, timedelta
    today = date_cls.today()
    all_fixtures: dict[int, dict] = {}
    for delta in range(-1, days_window + 1):
        d = today + timedelta(days=delta)
        try:
            fixtures = get_fixtures_by_date(league_id, season, str(d))
            for fx in fixtures:
                fid = fx.get("fixture", {}).get("id")
                if fid:
                    all_fixtures[fid] = fx
        except DailyLimitExceeded:
            break
    return list(all_fixtures.values())


def get_fixture_detail(fixture_id: int) -> dict:
    """获取单场比赛完整数据（含统计、事件、阵容）"""
    data = _request("fixtures", {"id": fixture_id}, "fixtures")
    results = data.get("response", [])
    return results[0] if results else {}


def get_standings(league_id: int, season: int) -> list[dict]:
    """获取联赛积分榜"""
    if not season:
        return []
    data = _request("standings", {"league": league_id, "season": season}, "standings")
    try:
        return data["response"][0]["league"]["standings"][0]
    except (IndexError, KeyError):
        return []


def get_team_statistics(team_id: int, league_id: int, season: int) -> dict:
    """获取球队本赛季统计"""
    if not team_id or not season:
        return {}
    data = _request(
        "teams/statistics",
        {"team": team_id, "league": league_id, "season": season},
        "team_statistics",
    )
    resp = data.get("response", {})
    # API 出错时 response 可能是空列表而非 dict
    return resp if isinstance(resp, dict) else {}


def get_injuries(fixture_id: int) -> list[dict]:
    """获取某场比赛的伤兵名单（需要 fixture_id）"""
    data = _request("injuries", {"fixture": fixture_id}, "injuries")
    return data.get("response", [])


def get_team_injuries(team_id: int, season: int) -> list[dict]:
    """
    获取球队本赛季伤兵/停赛名单（不需要 fixture_id）。
    API 参数: /injuries?team={id}&season={year}
    返回列表包含伤病类型、球员姓名、预计复出时间。
    """
    data = _request("injuries", {"team": team_id, "season": season}, "injuries")
    return data.get("response", [])


def get_head_to_head(team1_id: int, team2_id: int, last: Optional[int] = None) -> list[dict]:
    """获取两队历史对阵（last=None 则返回全部，free plan 不支持 last 参数）"""
    params: dict = {"h2h": f"{team1_id}-{team2_id}"}
    if last is not None:
        params["last"] = last
    data = _request("fixtures/headtohead", params, "head_to_head")
    return data.get("response", [])


def get_team_form(team_id: int, last: int = 5) -> list[dict]:
    """获取球队最近 N 场已完成比赛（免费计划：用日期范围替代 last 参数）"""
    from datetime import date as date_cls, timedelta
    today = date_cls.today()
    from_date = today - timedelta(days=180)
    data = _request(
        "fixtures",
        {"team": team_id, "from": str(from_date), "to": str(today), "status": "FT"},
        "fixtures",
    )
    results = data.get("response", [])
    results.sort(key=lambda x: x.get("fixture", {}).get("date", ""), reverse=True)
    return results[:last]


def search_team(name: str) -> list[dict]:
    """
    按名称搜索球队，返回 API-Football 球队列表（含 id、name、country、logo）。
    不依赖赛季参数，不消耗额外配额。
    """
    data = _request("teams", {"search": name}, "players")  # TTL 24h
    return [item.get("team", {}) for item in data.get("response", [])]


def get_coach(team_id: int) -> dict:
    """
    获取球队当前主教练信息（姓名、执教历史）。
    API 端点: /coachs?team={id}
    """
    data = _request("coachs", {"team": team_id}, "players")
    coaches = data.get("response", [])
    return coaches[0] if coaches else {}


def get_players(team_id: int, season: int) -> list[dict]:
    """获取球队球员名单及统计"""
    data = _request("players", {"team": team_id, "season": season}, "players")
    return data.get("response", [])


def get_finished_results(league_id: int, season: int, last: int = 5) -> list[dict]:
    """获取联赛最近 N 场已完成比赛（用于结果回填，免费计划使用日期范围）"""
    from datetime import date as date_cls, timedelta
    today = date_cls.today()
    from_date = today - timedelta(days=90)
    data = _request(
        "fixtures",
        {"league": league_id, "season": season, "from": str(from_date), "to": str(today), "status": "FT"},
        "results",
    )
    results = data.get("response", [])
    results.sort(key=lambda x: x.get("fixture", {}).get("date", ""), reverse=True)
    return results[:last]
