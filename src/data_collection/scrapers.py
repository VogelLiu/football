"""
多源爬虫模块：伤兵报告（SofaScore / FotMob / Transfermarkt）、赔率（Odds API）。
所有爬虫遵守 rate limiting（每域 3~6 秒间隔）。
"""
import random
import time
import unicodedata
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from src.logger import get_logger

logger = get_logger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _get_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _to_ascii_slug(name: str) -> str:
    """将名称中的非 ASCII 字符规范化为 ASCII（如 ü→u, ä→a, ö→o），再转为 URL slug。"""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().replace(" ", "-")


def _polite_sleep(min_s: float = 3.0, max_s: float = 6.0) -> None:
    """礼貌爬虫延迟，避免被封"""
    time.sleep(random.uniform(min_s, max_s))


def _fetch_html(url: str) -> Optional[str]:
    """同步 HTTP 请求，失败时返回 None"""
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers=_get_headers())
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        logger.warning("爬取失败 [%s]: %s", url, exc)
        return None


# ------------------------------------------------------------------ #
#  JSON API helper
# ------------------------------------------------------------------ #

def _json_get(url: str, extra_headers: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    """GET 一个 JSON endpoint，返回解析后的 dict 或失败时返回 None。"""
    headers = {**_get_headers(), **(extra_headers or {})}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.debug("JSON GET 失败 [%s]: %s", url, exc)
        return None


# ------------------------------------------------------------------ #
#  SofaScore injuries（非官方 Public API）
# ------------------------------------------------------------------ #

_SS_HEADERS = {
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Accept": "application/json",
}


def scrape_sofascore_injuries(team_name: str) -> list[dict]:
    """
    通过 SofaScore 非官方 API 获取球队当前伤兵 / 停赛名单。
    返回: [{"player": str, "type": str, "reason": str, "return_date": str, "source": "sofascore"}]
    """
    # 1. 搜索球队
    data = _json_get(
        f"https://api.sofascore.com/api/v1/search/all?q={urlencode({'q': team_name})}&page=0",
        _SS_HEADERS,
    )
    if not data:
        return []

    team_id: Optional[int] = None
    for item in data.get("results", []):
        if item.get("type") == "team":
            team_id = item.get("entity", {}).get("id")
            break

    if not team_id:
        return []

    # 2. 获取伤兵名单
    _polite_sleep(1, 2)
    inj_data = _json_get(
        f"https://api.sofascore.com/api/v1/team/{team_id}/injuries",
        _SS_HEADERS,
    )
    if not inj_data:
        return []

    results: list[dict] = []
    for entry in inj_data.get("data", []):
        player_info = entry.get("player", {})
        results.append({
            "player": player_info.get("name", "未知"),
            "type": entry.get("injuryType", ""),
            "reason": entry.get("description", ""),
            "return_date": entry.get("returnToTeamDate", ""),
            "source": "sofascore",
        })
    return results


# ------------------------------------------------------------------ #
#  FotMob injuries（非官方 Public API）
# ------------------------------------------------------------------ #

_FM_HEADERS = {
    "Referer": "https://www.fotmob.com/",
    "Accept": "application/json",
}


def scrape_fotmob_injuries(team_name: str) -> list[dict]:
    """
    通过 FotMob 非官方 API 获取球队伤兵名单。
    返回: [{"player": str, "type": str, "reason": str, "return_date": str, "source": "fotmob"}]
    """
    # 1. 搜索球队
    search_data = _json_get(
        f"https://www.fotmob.com/api/search?term={urlencode({'term': team_name})}&lang=en",
        _FM_HEADERS,
    )
    if not search_data:
        return []

    team_id: Optional[int] = None
    hits = search_data.get("hits", {})
    if isinstance(hits, dict):
        for category in hits.values():
            for hit in (category if isinstance(category, list) else []):
                if hit.get("type") == "team":
                    team_id = hit.get("id")
                    break
            if team_id:
                break
    elif isinstance(hits, list):
        for hit in hits:
            if hit.get("type") == "team":
                team_id = hit.get("id")
                break

    if not team_id:
        return []

    # 2. 获取球队详情（含伤兵）
    _polite_sleep(1, 2)
    team_data = _json_get(
        f"https://www.fotmob.com/api/teams?id={team_id}&ccode3=GBR&lang=en",
        _FM_HEADERS,
    )
    if not team_data:
        return []

    results: list[dict] = []
    squad = team_data.get("squad", {})
    for group in squad.get("members", []):
        for player in group.get("members", []):
            tag = player.get("injuryTag", {})
            if tag and tag.get("key") in ("injured", "doubtful", "suspended"):
                results.append({
                    "player": player.get("name", "未知"),
                    "type": tag.get("key", ""),
                    "reason": tag.get("localizedText", ""),
                    "return_date": player.get("expectedReturn", ""),
                    "source": "fotmob",
                })
    return results


# ------------------------------------------------------------------ #
#  Transfermarkt 伤兵 + 停赛（按球队 verletzungen / sperren 页）
# ------------------------------------------------------------------ #

_TM_HEADERS_EXTRA = {"Referer": "https://www.transfermarkt.com/"}
_TM_BASE = "https://www.transfermarkt.com"


def _tm_resolve_team(team_name: str, tm_headers: dict) -> tuple[str, str]:
    """
    在 Transfermarkt 搜索球队，返回 (slug, team_id)，失败时返回 ('', '')。
    例: ('fc-barcelona', '131')
    """
    search_url = (
        f"{_TM_BASE}/schnellsuche/ergebnis/schnellsuche"
        f"?query={urlencode({'query': team_name})}&Verein_page=0"
    )
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(search_url, headers=tm_headers)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.debug("Transfermarkt 搜索失败: %s", exc)
        return "", ""

    soup = BeautifulSoup(html, "lxml")
    link_tag = soup.select_one(
        "table.items td.hauptlink a[href*='/startseite/verein/']"
    ) or soup.select_one("table.items td.hauptlink a[href*='/verein/']")
    if not link_tag:
        return "", ""

    # href 例: /fc-barcelona/startseite/verein/131
    href = link_tag.get("href", "")
    parts = [p for p in href.split("/") if p]
    # parts: ['fc-barcelona', 'startseite', 'verein', '131']
    try:
        verein_idx = parts.index("verein")
        slug = parts[0]
        team_id = parts[verein_idx + 1]
        return slug, team_id
    except (ValueError, IndexError):
        return "", ""


def _tm_fetch_injuries(slug: str, team_id: str, tm_headers: dict) -> list[dict]:
    """
    爬取 Transfermarkt /verletzungen/ 页，返回当前伤兵列表。
    页面表格列: 球员 | 伤病时间 | 复出时间 | 伤病类型 | 缺席天数 | 错过比赛
    """
    url = f"{_TM_BASE}/{slug}/verletzungen/verein/{team_id}"
    _polite_sleep(2, 3)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers=tm_headers)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.debug("Transfermarkt 伤兵页爬取失败: %s", exc)
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    for row in soup.select("table.items tbody tr"):
        player_tag = row.select_one("td.hauptlink a")
        if not player_tag:
            continue
        cells = row.select("td")
        # 典型列序: [图标, 球员名/pos, 上阵次数, 伤病时间from, 复出时间, 伤病类型, 天数, 场次]
        # 伤病类型通常在 index 5（不同版本页面列数略有差异）
        inj_type = ""
        return_date = ""
        if len(cells) >= 5:
            # 最后一列通常是缺席场次，倒数第二列或第三列为复出时间
            # 遍历找含日期格式的单元格作为复出时间
            for cell in cells:
                txt = cell.get_text(strip=True)
                # 简单启发：含"-"且短字符串可能是日期或 "-"（无复出时间）
                if len(txt) <= 12 and ("." in txt or txt == "-"):
                    return_date = txt if txt != "-" else ""
            # 伤病类型：跳过球员名格后第一个有实质内容的格
            for i, cell in enumerate(cells[2:], 2):
                txt = cell.get_text(strip=True)
                if txt and not txt.isdigit() and "." not in txt and len(txt) > 2:
                    inj_type = txt
                    break

        results.append({
            "player": player_tag.get_text(strip=True),
            "type": "injured",
            "reason": inj_type,
            "return_date": return_date,
            "source": "transfermarkt",
        })
        if len(results) >= 10:
            break
    return results


def _tm_fetch_suspensions(slug: str, team_id: str, tm_headers: dict) -> list[dict]:
    """
    爬取 Transfermarkt /sperren/ 页（停赛状态），返回停赛球员列表。
    页面表格列: 球员 | 赛事 | 停赛场次 | 剩余场次 | 原因
    """
    url = f"{_TM_BASE}/{slug}/sperren/verein/{team_id}"
    _polite_sleep(1, 2)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers=tm_headers)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.debug("Transfermarkt 停赛页爬取失败: %s", exc)
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []
    for row in soup.select("table.items tbody tr"):
        player_tag = row.select_one("td.hauptlink a")
        if not player_tag:
            continue
        cells = row.select("td")
        competition = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        remaining = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        reason = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        results.append({
            "player": player_tag.get_text(strip=True),
            "type": "suspended",
            "reason": reason or competition,
            "return_date": f"还剩 {remaining} 场" if remaining and remaining != "0" else "",
            "source": "transfermarkt",
        })
        if len(results) >= 5:
            break
    return results


def scrape_transfermarkt_injuries(team_name: str) -> list[dict]:
    """
    从 Transfermarkt 获取球队当前伤兵 + 停赛名单（两个页面合并）。
    - 伤兵来源: /verletzungen/verein/{id}
    - 停赛来源: /sperren/verein/{id}
    返回: [{"player": str, "type": "injured"|"suspended", "reason": str,
             "return_date": str, "source": "transfermarkt"}]
    """
    tm_headers = {**_get_headers(), **_TM_HEADERS_EXTRA}

    slug, team_id = _tm_resolve_team(team_name, tm_headers)
    if not slug or not team_id:
        logger.debug("Transfermarkt 未找到球队: %s", team_name)
        return []

    injuries = _tm_fetch_injuries(slug, team_id, tm_headers)
    suspensions = _tm_fetch_suspensions(slug, team_id, tm_headers)

    combined = injuries + suspensions
    if combined:
        logger.info(
            "Transfermarkt: %s 共 %d 伤兵 + %d 停赛",
            team_name, len(injuries), len(suspensions),
        )
    return combined


# ------------------------------------------------------------------ #
#  聚合入口：伤兵报告
# ------------------------------------------------------------------ #

# football-data.org 全名 → 各爬虫更易识别的短名（或别名）
_INJURY_SEARCH_ALIAS: dict[str, str] = {
    "Paris Saint-Germain FC":          "Paris Saint-Germain",
    "FC Bayern München":               "Bayern Munich",
    "Borussia Dortmund":               "Dortmund",
    "Bayer 04 Leverkusen":             "Bayer Leverkusen",
    "Eintracht Frankfurt":             "Frankfurt",
    "FC Internazionale Milano":        "Inter Milan",
    "Juventus FC":                     "Juventus",
    "SSC Napoli":                      "Napoli",
    "Atalanta BC":                     "Atalanta",
    "AS Monaco FC":                    "Monaco",
    "Olympique de Marseille":          "Marseille",
    "Olympique Lyonnais":              "Lyon",
    "Real Madrid CF":                  "Real Madrid",
    "FC Barcelona":                    "Barcelona",
    "Club Atlético de Madrid":         "Atletico Madrid",
    "Athletic Club":                   "Athletic Bilbao",
    "Villarreal CF":                   "Villarreal",
    "Arsenal FC":                      "Arsenal",
    "Chelsea FC":                      "Chelsea",
    "Manchester City FC":              "Manchester City",
    "Manchester United FC":            "Manchester United",
    "Liverpool FC":                    "Liverpool",
    "Tottenham Hotspur FC":            "Tottenham",
    "Aston Villa FC":                  "Aston Villa",
    "AFC Ajax":                        "Ajax",
    "PSV Eindhoven":                   "PSV",
    "Feyenoord Rotterdam":             "Feyenoord",
    "SL Benfica":                      "Benfica",
    "FC Porto":                        "Porto",
    "Sporting Clube de Portugal":      "Sporting CP",
    "Sporting CP":                     "Sporting CP",
}


def _injury_search_names(team_name: str) -> list[str]:
    """
    返回依次尝试的搜索名列表（首选别名，其次去掉常见后缀，最后保留原名）。
    去重并保持顺序。
    """
    import re
    names: list[str] = []

    # 1. 已知别名优先
    if team_name in _INJURY_SEARCH_ALIAS:
        names.append(_INJURY_SEARCH_ALIAS[team_name])

    # 2. 去掉常见前缀/后缀得到短名
    short = re.sub(
        r"\b(FC|CF|AC|AS|SC|SL|FK|SK|BK|RFC|AFC|SFC|SSC|RB|PSV|PSG|BC|US|VfL|VfB|FSV|TSG)\.?\b",
        "",
        team_name,
        flags=re.IGNORECASE,
    ).strip()
    short = re.sub(r"\s{2,}", " ", short).strip()
    if short and short != team_name and short not in names:
        names.append(short)

    # 3. 原名兜底
    if team_name not in names:
        names.append(team_name)

    return names


def collect_injury_reports(team_name: str) -> list[dict]:
    """
    聚合多来源伤兵报告：SofaScore → FotMob → Transfermarkt。
    每个来源依次尝试别名 / 短名 / 原名，首个有数据的来源即返回。
    全部失败则返回空列表（调用方决定是否回退 API）。
    统一格式: [{"player": str, "type": str, "reason": str, "return_date": str, "source": str}]
    """
    search_names = _injury_search_names(team_name)
    logger.info(
        "爬取 %s 伤兵信息（SofaScore / FotMob / Transfermarkt），搜索名: %s",
        team_name, search_names,
    )
    for scraper_fn, label in [
        (scrape_sofascore_injuries, "SofaScore"),
        (scrape_fotmob_injuries, "FotMob"),
        (scrape_transfermarkt_injuries, "Transfermarkt"),
    ]:
        for sname in search_names:
            try:
                injuries = scraper_fn(sname)
                if injuries:
                    logger.info(
                        "%s 找到 %d 条伤兵记录（%s → 搜索 '%s'）",
                        label, len(injuries), team_name, sname,
                    )
                    return injuries
            except Exception as exc:
                logger.warning("%s 伤兵爬取异常 (%s): %s", label, sname, exc)

    logger.info("所有外部来源均未找到 %s 的伤兵信息", team_name)
    return []


# ------------------------------------------------------------------ #
#  The Odds API — Bet365 赔率
# ------------------------------------------------------------------ #

# API Football league_id → The Odds API sport key
_ODDS_API_SPORT_KEY: dict[int, str] = {
    39:  "soccer_england_premier_league",
    140: "soccer_spain_la_liga",
    78:  "soccer_germany_bundesliga",
    135: "soccer_italy_serie_a",
    61:  "soccer_france_ligue_one",
    2:   "soccer_uefa_champs_league",
    169: "soccer_china_super_league",
}

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def _parse_odds_event(event: dict) -> Optional[dict]:
    """解析 The Odds API 事件，提取 1X2 和大小球赔率。"""
    result: dict = {}
    home_name = event.get("home_team", "")
    away_name = event.get("away_team", "")
    for bm in event.get("bookmakers", []):
        if bm.get("key") != "bet365":
            continue
        for market in bm.get("markets", []):
            key = market.get("key", "")
            outcomes = market.get("outcomes", [])
            if key == "h2h":
                for o in outcomes:
                    price = o.get("price", 0)
                    name = o.get("name", "")
                    if name == home_name:
                        result["home_win"] = price
                    elif name == away_name:
                        result["away_win"] = price
                    elif name == "Draw":
                        result["draw"] = price
            elif key == "totals":
                for o in outcomes:
                    point = float(o.get("point") or 0)
                    if abs(point - 2.5) < 0.01:
                        if o.get("name") == "Over":
                            result["over_25"] = o.get("price", 0)
                        elif o.get("name") == "Under":
                            result["under_25"] = o.get("price", 0)
    return result if result else None


def fetch_bet365_odds(
    home_team: str, away_team: str, league_id: int = 0
) -> Optional[dict]:
    """
    通过 The Odds API 获取 Bet365 赔率（1X2 + 大小球 2.5）。
    需在 .env 中配置 ODDS_API_KEY（免费 500次/月，网址：https://the-odds-api.com/）。
    返回: {"home_win": float, "draw": float, "away_win": float,
             "over_25": float, "under_25": float, "bookmaker": "Bet365"}
    """
    from src.config import settings
    api_key = settings.odds_api_key
    if not api_key:
        logger.debug(
            "未配置 ODDS_API_KEY，跳过赔率查询"
            "（在 .env 中添加 ODDS_API_KEY=... 启用 Bet365 赔率）"
        )
        return None

    sport_key = _ODDS_API_SPORT_KEY.get(league_id, "soccer")
    url = f"{_ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h,totals",
        "bookmakers": "bet365",
        "oddsFormat": "decimal",
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
        if resp.status_code in (401, 403):
            logger.warning("Odds API 认证失败，请检查 ODDS_API_KEY")
            return None
        if resp.status_code == 422:
            logger.debug("Odds API: 联赛 %s 无数据", sport_key)
            return None
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:
        logger.debug("Odds API 请求失败: %s", exc)
        return None

    def _clean(name: str) -> str:
        return name.lower().replace(" fc", "").replace(" cf", "").strip()

    home_key = _clean(home_team)
    away_key = _clean(away_team)

    for event in events:
        ev_home_k = _clean(event.get("home_team", ""))
        ev_away_k = _clean(event.get("away_team", ""))
        if (
            (home_key in ev_home_k or ev_home_k in home_key) and
            (away_key in ev_away_k or ev_away_k in away_key)
        ):
            odds = _parse_odds_event(event)
            if odds:
                odds["bookmaker"] = "Bet365"
                return odds

    logger.debug("Odds API 未找到匹配比赛: %s vs %s", home_team, away_team)
    return None
