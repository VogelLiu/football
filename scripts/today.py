"""
交互式当日预测脚本。

流程：
  1. 通过 football-data.org API 获取当日赛程
  2. 用户选择场次 + 博彩市场
  3. 自动从 API-Football 拉取历史统计数据（上赛季）
  4. LLM 综合分析并展示结果

用法：
    python scripts/today.py
    python scripts/today.py --date 2026-04-08     # 指定日期
    python scripts/today.py --league 39           # 只看英超
"""
import argparse
import sys
import warnings
from datetime import datetime, date as date_cls, timedelta
from pathlib import Path
from typing import Optional

# 在导入 langchain 之前抑制 Pydantic v1 兼容性警告（Python 3.14+）
warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality", category=UserWarning)
warnings.filterwarnings("ignore", module=r"pydantic\.v1", category=UserWarning)

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from src.config import settings, LEAGUE_NAMES
from src.data_collection import search_team, get_remaining_quota, DailyLimitExceeded
from src.data_collection.sources import LEAGUE_BY_ID, TEAM_NAME_CN
from src.agent.agent_loop import predict_by_team_names
from src.logger import get_logger
from src.models import init_db, check_disk_available

logger = get_logger("today")
console = Console()

# ------------------------------------------------------------------ #
#  联赛 & 市场配置
# ------------------------------------------------------------------ #
LEAGUE_ORDER = [39, 140, 78, 135, 61, 169, 2]

LEAGUE_INFO = {
    39:  {"name": "英超",  "en": "Premier League",   "country": "England"},
    140: {"name": "西甲",  "en": "La Liga",           "country": "Spain"},
    78:  {"name": "德甲",  "en": "Bundesliga",        "country": "Germany"},
    135: {"name": "意甲",  "en": "Serie A",           "country": "Italy"},
    61:  {"name": "法甲",  "en": "Ligue 1",           "country": "France"},
    169: {"name": "中超",  "en": "Chinese Super League", "country": "China"},
    2:   {"name": "欧冠",  "en": "Champions League",  "country": "Europe"},
}

MARKETS = {
    "1": ("1X2",       "胜平负（主胜 / 平 / 客胜）"),
    "2": ("score",     "比分预测"),
    "3": ("ou_25",     "大小球（2.5 球线）"),
    "4": ("asian_hcp", "亚盘让球"),
    "5": ("btts",      "两队都进球（BTTS）"),
    "6": ("半场",      "半场胜负（上半场 1X2）"),
    "0": ("all",       "全部市场（AI 自行推荐）"),
}

# football-data.org 联赛代码映射（免费 tier 支持）
# 键: API-Football league_id  →  值: football-data.org competition code
_FD_CODE: dict[int, str] = {
    39:  "PL",   # 英超
    140: "PD",   # 西甲
    78:  "BL1",  # 德甲
    135: "SA",   # 意甲
    61:  "FL1",  # 法甲
    2:   "CL",   # 欧冠
    # 中超在 football-data.org 免费层未覆盖
}

_FD_BASE = "https://api.football-data.org/v4"

# 杯赛/欧冠球队所属国内联赛（API-Football league_id）
# 用于当 league_id=2（欧冠）时，将球队统计 fallback 到其所在国内联赛
_DOMESTIC_LEAGUE: dict[str, int] = {
    # 英超球队
    "Arsenal FC": 39, "Arsenal": 39,
    "Liverpool FC": 39, "Liverpool": 39,
    "Manchester City FC": 39, "Manchester City": 39,
    "Manchester United FC": 39, "Manchester United": 39,
    "Chelsea FC": 39, "Chelsea": 39,
    "Tottenham Hotspur FC": 39, "Tottenham": 39,
    "Aston Villa FC": 39, "Aston Villa": 39,
    # 西甲球队
    "Real Madrid CF": 140, "Real Madrid": 140,
    "FC Barcelona": 140, "Barcelona": 140,
    "Club Atlético de Madrid": 140, "Atletico Madrid": 140,
    "Sevilla FC": 140, "Sevilla": 140,
    "Athletic Club": 140,
    "Villarreal CF": 140,
    # 德甲球队
    "FC Bayern München": 78, "Bayern Munich": 78,
    "Borussia Dortmund": 78,
    "RB Leipzig": 78,
    "Bayer 04 Leverkusen": 78, "Bayer Leverkusen": 78,
    "Eintracht Frankfurt": 78,
    # 意甲球队
    "FC Internazionale Milano": 135, "Inter": 135,
    "AC Milan": 135,
    "Juventus FC": 135, "Juventus": 135,
    "SSC Napoli": 135, "Napoli": 135,
    "Atalanta BC": 135, "Atalanta": 135,
    # 法甲球队
    "Paris Saint-Germain FC": 61, "Paris Saint-Germain": 61,
    "AS Monaco FC": 61, "Monaco": 61,
    "Olympique de Marseille": 61, "Marseille": 61,
    # 葡超球队
    "Sporting Clube de Portugal": 94,  # Primeira Liga
    "SL Benfica": 94,
    "FC Porto": 94,
    # 荷甲球队
    "AFC Ajax": 88,  # Eredivisie
    "PSV Eindhoven": 88,
    "Feyenoord Rotterdam": 88,
}


# ------------------------------------------------------------------ #
#  Step 1：通过 football-data.org 获取当日赛程
# ------------------------------------------------------------------ #
def fetch_matches_from_football_data(
    target_date: date_cls, league_ids: list[int]
) -> tuple[list[dict], str]:
    """
    通过 football-data.org /v4/matches API 获取当日指定联赛的赛程。
    返回: (matches: list[dict], error_msg: str)
      - matches 为正常结果列表
      - error_msg 非空表示出错原因；空字符串 + 空列表表示当日无比赛
    """
    api_key = settings.football_data_key
    if not api_key:
        return [], (
            "未配置 FOOTBALL_DATA_KEY，请在 .env 中添加。\n"
            "到 https://www.football-data.org/client/register 免费注册后写入 Key。"
        )

    fd_codes = [_FD_CODE[lid] for lid in league_ids if lid in _FD_CODE]
    if not fd_codes:
        return [], "所选联赛均不在 football-data.org 免费层支持范围内。"

    url = f"{_FD_BASE}/matches"
    # football-data.org 免费层在 dateFrom==dateTo 且带 competitions 时返回空，
    # 用 dateTo +1 天规避，再在客户端过滤回目标日期。
    next_date = target_date + timedelta(days=1)
    params = {
        "dateFrom": str(target_date),
        "dateTo": str(next_date),
        "competitions": ",".join(fd_codes),
    }
    headers = {"X-Auth-Token": api_key}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params, headers=headers)
        if resp.status_code == 403:
            return [], "football-data.org 返回 403 Forbidden，请检查 FOOTBALL_DATA_KEY 是否正确。"
        if resp.status_code == 429:
            return [], "football-data.org 请求频率过高（免费层10次/分钟），请稍后重试。"
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        return [], f"football-data.org 请求失败: HTTP {exc.response.status_code}"
    except Exception as exc:
        return [], f"football-data.org 请求异常: {exc}"

    # 只保留目标日期（UTC）的比赛
    target_str = str(target_date)
    raw_matches = [m for m in data.get("matches", []) if m.get("utcDate", "").startswith(target_str)]
    if not raw_matches:
        return [], ""  # 空列表但无错误 → 当日真的无比赛

    result: list[dict] = []
    for m in raw_matches:
        comp = m.get("competition", {})
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        utc_date = m.get("utcDate", "")  # 格式: "2026-04-07T19:45:00Z"
        time_utc = utc_date[11:16] if len(utc_date) >= 16 else "TBD"
        fd_code = comp.get("code", "")
        league_id = next((lid for lid, code in _FD_CODE.items() if code == fd_code), 0)
        result.append({
            "league": LEAGUE_INFO.get(league_id, {}).get("name", comp.get("name", "?")),
            "league_id": league_id,
            "home_team": home.get("name", "?"),
            "home_team_cn": TEAM_NAME_CN.get(home.get("name", ""), ""),
            "away_team": away.get("name", "?"),
            "away_team_cn": TEAM_NAME_CN.get(away.get("name", ""), ""),
            "time_utc": time_utc,
            "venue": m.get("venue", "") or "",
            "round": str(m.get("matchday") or m.get("stage") or ""),
            "status": m.get("status", ""),
        })
    return result, ""


# ------------------------------------------------------------------ #
#  Step 2：展示比赛列表，用户选择
# ------------------------------------------------------------------ #
def display_and_select_match(matches: list[dict], target_date: date_cls) -> Optional[dict]:
    if not matches:
        return None

    table = Table(
        title=f"📅 {target_date}  今日比赛（来源: football-data.org）",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("#",      style="bold white", justify="right", min_width=3)
    table.add_column("联赛",   min_width=6)
    table.add_column("时间(UTC)", min_width=8, justify="center")
    table.add_column("主队",   min_width=20)
    table.add_column("客队",   min_width=20)
    table.add_column("场地",   min_width=16)
    table.add_column("轮次",   min_width=8)

    for idx, m in enumerate(matches, 1):
        home_cn = m.get("home_team_cn") or TEAM_NAME_CN.get(m.get("home_team", ""), m.get("home_team", "?"))
        away_cn = m.get("away_team_cn") or TEAM_NAME_CN.get(m.get("away_team", ""), m.get("away_team", "?"))
        table.add_row(
            str(idx),
            m.get("league", ""),
            m.get("time_utc", "TBD"),
            home_cn,
            away_cn,
            (m.get("venue") or "-")[:18],
            m.get("round") or "-",
        )

    console.print(table)
    console.print(
        "[dim]ℹ️  赛程数据来源: football-data.org API（实时数据，时间为 UTC）[/dim]"
    )

    console.print("\n[bold]输入 M 手动输入比赛，或输入编号选择：[/bold]")
    while True:
        choice = Prompt.ask(f"编号 (1-{len(matches)}) 或 M", default="1")
        if choice.strip().upper() == "M":
            return _manual_match_input()
        try:
            idx = int(choice)
            if 1 <= idx <= len(matches):
                return matches[idx - 1]
        except ValueError:
            pass
        console.print(f"[red]请输入 1-{len(matches)} 或 M[/red]")


def _manual_match_input() -> dict:
    """让用户手动输入比赛信息"""
    console.print("\n[bold cyan]手动输入比赛信息[/bold cyan]")
    home = Prompt.ask("主队英文名（用于 API 搜索，如 Bayern Munich）")
    away = Prompt.ask("客队英文名（如 Real Madrid）")
    home_cn = Prompt.ask("主队中文名（可选，直接 Enter 跳过）", default="")
    away_cn = Prompt.ask("客队中文名（可选，直接 Enter 跳过）", default="")

    # 展示联赛列表供选择
    t = Table(show_header=False, border_style="dim")
    t.add_column("ID",   style="bold yellow", min_width=5, justify="right")
    t.add_column("联赛", style="bold white",  min_width=8)
    t.add_column("英文", style="dim",         min_width=20)
    for lid, info in LEAGUE_INFO.items():
        t.add_row(str(lid), info["name"], info["en"])
    console.print(t)

    league_id_str = Prompt.ask("联赛 ID（参见上表）", default="39")
    try:
        league_id = int(league_id_str)
    except ValueError:
        league_id = 39
    league = LEAGUE_INFO.get(league_id, {}).get("name", str(league_id))
    time_utc = Prompt.ask("比赛时间 UTC（如 19:45，不知道请直接 Enter）", default="TBD")

    return {
        "league": league,
        "league_id": league_id,
        "home_team": home,
        "home_team_cn": home_cn or home,
        "away_team": away,
        "away_team_cn": away_cn or away,
        "time_utc": time_utc,
        "venue": "",
        "round": "",
    }


# ------------------------------------------------------------------ #
#  Step 3：用户选择博彩市场
# ------------------------------------------------------------------ #
def select_markets() -> list[str]:
    console.print("\n[bold cyan]请选择关注的博彩市场（可多选，逗号分隔）：[/bold cyan]")
    t = Table(show_header=False, border_style="dim")
    t.add_column("编号", style="bold yellow", min_width=4)
    t.add_column("市场",  style="bold white",  min_width=16)
    t.add_column("说明",  style="dim")
    for key, (mid, desc) in MARKETS.items():
        t.add_row(f"[{key}]", mid.upper(), desc)
    console.print(t)

    while True:
        raw = Prompt.ask("\n输入编号（如 1,3 或 0=全部）", default="0")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not keys:
            console.print("[red]至少选择一个[/red]")
            continue
        if "0" in keys:
            return [v[0] for k, v in MARKETS.items() if k != "0"]
        valid = [k for k in keys if k in MARKETS and k != "0"]
        bad   = [k for k in keys if k not in MARKETS]
        if bad:
            console.print(f"[red]无效编号: {', '.join(bad)}[/red]")
            continue
        return [MARKETS[k][0] for k in valid]


# ------------------------------------------------------------------ #
#  Step 4：解析 team_id（通过 API Football 搜索，仅1-2次调用）
# ------------------------------------------------------------------ #
def resolve_team_id(team_name: str) -> Optional[int]:
    """搜索球队名返回 API Football team_id，消耗 1 次配额。

    API Football 的 /teams?search= 只接受字母数字和空格，不接受连字符、
    "FC"/"CF"/"AC" 等缀词。此函数先对名称做清洗再搜索。
    """
    import re
    import unicodedata

    # 将带音标的 Unicode 字符转为 ASCII（é→e, ü→u 等），令 API 可接受
    normalized = unicodedata.normalize("NFD", team_name)
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")

    # 去掉常见机构缀词（顺序：先去长后缀再去短后缀）
    clean = re.sub(
        r"\b(1846|1909|1910|1913|1907|RFC|AFC|SFC|SL|SC|FC|CF|AC|BC|AS|US|FK|SK|BK|VfL|VfB|FSV|TSG|SSC|RB|PSV|PSG)\.?\b",
        "",
        ascii_name,
        flags=re.IGNORECASE,
    ).strip()

    # 将连字符替换为空格，去掉其他非字母数字空格字符
    clean = re.sub(r"[-]", " ", clean)
    clean = re.sub(r"[^\w\s]", "", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip()

    # 如果清洗后名称变太短（<3字符），直接用原名（截到40字符）
    search_name = clean if len(clean) >= 3 else team_name[:40]

    try:
        results = search_team(search_name)
        if results:
            return results[0].get("id")
        # 如果清洗后搜不到，回退到原名（去掉 " FC/" 之类尾缀）
        if clean != team_name:
            results = search_team(team_name.split(" FC")[0].split(" CF")[0][:40])
            if results:
                return results[0].get("id")
    except DailyLimitExceeded:
        logger.warning("API 配额已耗尽，无法解析 team_id: %s", team_name)
    except Exception as exc:
        logger.warning("搜索球队 %s 失败: %s", team_name, exc)
    return None


# ------------------------------------------------------------------ #
#  Step 5a：数据收集摘要面板
# ------------------------------------------------------------------ #
def print_data_summary(home_cn: str, away_cn: str, data_summary: dict) -> None:
    """在预测结果前展示数据覆盖情况。"""
    if not data_summary:
        return

    def _icon(ok: bool) -> str:
        return "[green]✓[/green]" if ok else "[red]✗[/red]"

    rows = [
        (f"主队排名（{home_cn}）",      data_summary.get("home_rank", False)),
        (f"客队排名（{away_cn}）",      data_summary.get("away_rank", False)),
        (f"主队赛季统计（{home_cn}）",  data_summary.get("home_stats", False)),
        (f"客队赛季统计（{away_cn}）",  data_summary.get("away_stats", False)),
        (f"主队近期状态（{home_cn}）",  data_summary.get("home_form", False)),
        (f"客队近期状态（{away_cn}）",  data_summary.get("away_form", False)),
        (f"主队伤兵数据（{home_cn}）",  data_summary.get("home_injuries", False)),
        (f"客队伤兵数据（{away_cn}）",  data_summary.get("away_injuries", False)),
        ("历史对阵 H2H（近5年）",       data_summary.get("h2h", False)),
        ("Bet365 赔率",                 data_summary.get("odds", False)),
    ]

    tbl = Table(show_header=False, border_style="dim", box=None, padding=(0, 1))
    tbl.add_column("数据项", style="dim", min_width=28)
    tbl.add_column("状态", justify="center", min_width=4)
    for label, ok in rows:
        tbl.add_row(label, _icon(ok))

    missing_count = sum(1 for _, ok in rows if not ok)
    subtitle = (
        f"⚠️ 缺失数据 {missing_count} 项，LLM 将基于已有信息推断，置信度可能偏低"
        if missing_count else None
    )
    console.print(Panel(
        tbl,
        title=f"📊 数据覆盖摘要  {home_cn} vs {away_cn}",
        border_style="cyan",
        subtitle=subtitle,
    ))


# ------------------------------------------------------------------ #
#  Step 5：展示预测结果
# ------------------------------------------------------------------ #
def print_prediction(
    home_cn: str,
    away_cn: str,
    prediction,        # MatchPredictionOutput
    selected_markets: list[str],
) -> None:
    console.print()
    console.print(Panel(
        f"[bold white]{home_cn}[/bold white]  [cyan]VS[/cyan]  [bold white]{away_cn}[/bold white]",
        title="⚽ 预测结果",
        border_style="green",
    ))

    MARKET_RENDER = {
        "1X2":       ("pred_1x2",      lambda p: {"1": "主胜", "X": "平局", "2": "客胜"}.get(p.get("prediction", "?"), p.get("prediction", "?")), "1=主胜 X=平 2=客胜"),
        "score":     ("pred_score",     lambda p: f"{p.get('home','?')}-{p.get('away','?')}", "预测比分"),
        "ou_25":     ("pred_ou_25",     lambda p: "大球 (over)" if p.get("side") == "over" else "小球 (under)", "2.5 球线"),
        "asian_hcp": ("pred_asian_hcp", lambda p: f"{'主队' if p.get('side')=='home' else '客队'} {p.get('line','?')}", "亚盘让球"),
        "btts":      ("pred_btts",      lambda p: "两队均进" if p.get("prediction") == "yes" else "非双进", "BTTS"),
        "half":      (None, None, "半场胜负（参见推理文本）"),
    }

    DISPLAY_NAMES = {
        "1X2": "胜平负 (1X2)", "score": "比分预测", "ou_25": "大小球 2.5",
        "asian_hcp": "亚盘让球", "btts": "两队进球 (BTTS)", "half": "半场胜负",
    }

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("市场",    style="bold white", min_width=16)
    table.add_column("预测",    justify="center",   min_width=16)
    table.add_column("置信度",  justify="center",   min_width=10)
    table.add_column("备注",    min_width=20)

    for mkt in selected_markets:
        label = DISPLAY_NAMES.get(mkt, mkt)
        info  = MARKET_RENDER.get(mkt)
        if not info or info[0] is None:
            table.add_row(label, "[dim]见推理文本[/dim]", "-", info[2] if info else "")
            continue
        attr, fmt, note = info
        raw = getattr(prediction, attr, None)
        if raw is None:
            table.add_row(label, "[dim]暂无[/dim]", "-", note)
            continue
        data = raw if isinstance(raw, dict) else (raw.model_dump() if hasattr(raw, "model_dump") else {})
        conf = data.get("confidence", 0)
        color = "green" if conf >= 0.65 else ("yellow" if conf >= 0.5 else "red")
        table.add_row(label, fmt(data), f"[{color}]{conf:.1%}[/{color}]", note)

    console.print(table)

    console.print(Panel(
        f"[bold yellow]最推荐市场[/bold yellow]: {prediction.recommended_market}\n"
        f"[bold green]{prediction.recommended_detail}[/bold green]",
        title="📌 AI 推荐", border_style="yellow",
    ))

    if prediction.key_factors:
        console.print(Panel(
            "\n".join(f"  • {f}" for f in prediction.key_factors),
            title="🔑 关键因素", border_style="blue",
        ))

    if prediction.reasoning:
        console.print(Panel(
            prediction.reasoning,
            title="🔍 AI 分析推理", border_style="dim blue",
        ))

    if prediction.data_quality_note:
        console.print(f"[dim]⚠️  数据备注: {prediction.data_quality_note}[/dim]")


# ------------------------------------------------------------------ #
#  主函数
# ------------------------------------------------------------------ #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="交互式今日足球预测（用户指定比赛 + API 历史数据）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/today.py                       # 今日所有联赛
  python scripts/today.py --date 2026-04-08     # 指定日期
  python scripts/today.py --league 39           # 只看英超
  python scripts/today.py --league 78 140       # 德甲+西甲
  python scripts/today.py --stats-season 2024   # 指定历史数据赛季
""",
    )
    parser.add_argument("--date",   type=str, help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--league", type=int, nargs="+", help="联赛 ID（可多个）")
    parser.add_argument("--stats-season", type=int, help="历史统计赛季（默认上赛季, 如 2024）")
    args = parser.parse_args()

    if not check_disk_available():
        sys.exit(1)
    init_db()

    # 确定目标日期
    if args.date:
        try:
            target_date = date_cls.fromisoformat(args.date)
        except ValueError:
            console.print("[red]日期格式错误，请用 YYYY-MM-DD[/red]")
            sys.exit(1)
    else:
        target_date = date_cls.today()

    # 确定赛季
    default_season = target_date.year if target_date.month >= 8 else target_date.year - 1
    stats_season = args.stats_season or (default_season - 1)  # 上赛季历史数据

    # 确定联赛列表
    if args.league:
        league_ids = args.league
    else:
        league_ids = sorted(
            settings.league_ids,
            key=lambda x: LEAGUE_ORDER.index(x) if x in LEAGUE_ORDER else 99,
        )

    league_labels = " / ".join(LEAGUE_INFO.get(lid, {}).get("name", str(lid)) for lid in league_ids)
    console.print(Panel(
        f"[bold]日期[/bold]: {target_date}   "
        f"[bold]联赛[/bold]: {league_labels}   "
        f"[bold]历史数据赛季[/bold]: {stats_season}   "
        f"[bold]剩余 API 配额[/bold]: {get_remaining_quota()}",
        title="⚽ 足球预测助手",
        border_style="cyan",
    ))

    # ── Step 1: 从 football-data.org 获取今日赛程 ──
    with console.status("[bold green]正在从 football-data.org 获取今日赛程..."):
        matches, error_msg = fetch_matches_from_football_data(target_date, league_ids)

    if error_msg:
        # API 出错（无 Key、403、网络异常等）
        console.print(f"\n[red]⚠️  {error_msg}[/red]")
        console.print("[yellow]回退到手动输入模式。[/yellow]\n")
        selected = _manual_match_input()
    elif not matches:
        # API 正常但无比赛
        console.print(
            f"\n[bold yellow]⚽ {target_date} 今日所关注联赛暂无赛事安排：[/bold yellow]\n"
            f"  {league_labels}\n"
            "[dim]（可能为休赛日、国际赛事周或赛程空档期）[/dim]\n"
        )
        if not Confirm.ask("仍要手动输入一场比赛进行分析？", default=False):
            sys.exit(0)
        selected = _manual_match_input()
    else:
        # ── Step 2: 用户选择比赛 ──
        selected = display_and_select_match(matches, target_date)
        if selected is None:
            sys.exit(0)

    home_en  = selected.get("home_team", "")
    away_en  = selected.get("away_team", "")
    home_cn  = selected.get("home_team_cn") or TEAM_NAME_CN.get(home_en, home_en)
    away_cn  = selected.get("away_team_cn") or TEAM_NAME_CN.get(away_en, away_en)
    league_id   = selected.get("league_id", league_ids[0] if league_ids else 39)
    league_name = selected.get("league") or LEAGUE_INFO.get(league_id, {}).get("name", str(league_id))
    time_str    = selected.get("time_utc", "TBD")

    console.print(
        f"\n[bold green]✓ 已选择[/bold green]: [{league_name}] "
        f"[bold]{home_cn} vs {away_cn}[/bold]  {time_str} UTC"
    )

    # ── Step 3: 选择博彩市场 ──
    selected_markets = select_markets()
    console.print(
        f"\n[bold green]✓ 关注市场[/bold green]: "
        f"{', '.join(m.upper() for m in selected_markets)}"
    )

    # ── 确认 ──
    if not Confirm.ask("\n[bold]开始收集历史数据并分析？[/bold]", default=True):
        console.print("[dim]已取消[/dim]")
        sys.exit(0)

    # ── Step 4: 解析 team_id（消耗 1-2 次 API 配额，用于后续历史数据查询）──
    home_id: Optional[int] = None
    away_id: Optional[int] = None
    with console.status("[bold green]解析球队 ID（API Football team search）..."):
        if get_remaining_quota() >= 2:
            home_id = resolve_team_id(home_en)
            away_id = resolve_team_id(away_en)
            status_parts = []
            if home_id:
                status_parts.append(f"{home_cn}(id={home_id})")
            else:
                status_parts.append(f"{home_cn}(未找到，将跳过部分统计)")
            if away_id:
                status_parts.append(f"{away_cn}(id={away_id})")
            else:
                status_parts.append(f"{away_cn}(未找到，将跳过部分统计)")
            console.print(f"  [dim]{' | '.join(status_parts)}[/dim]")
        else:
            console.print("[yellow]  API 配额不足，跳过 team_id 解析，将仅使用 LLM 知识分析[/yellow]")

    # ── Step 5: 历史数据收集 + LLM 分析 ──
    match_date = datetime.combine(target_date, datetime.min.time())
    if time_str and time_str != "TBD":
        try:
            h, m = map(int, time_str.split(":"))
            match_date = match_date.replace(hour=h, minute=m)
        except Exception:
            pass

    console.print(
        f"\n[bold green]正在收集历史数据...[/bold green]\n"
        f"  [dim]来源: API-Football ({stats_season}赛季统计) + 中性伤病爬虫 + Bet365 赔率[/dim]"
    )

    # 杯赛（欧冠等）无法通过 league_id 查到球队统计，fallback 到国内联赛
    home_stats_league = _DOMESTIC_LEAGUE.get(home_en) or league_id
    away_stats_league = _DOMESTIC_LEAGUE.get(away_en) or league_id

    try:
        prediction, data_summary = predict_by_team_names(
            home_name=home_en,
            away_name=away_en,
            league_id=league_id,
            league_name=league_name,
            match_date=match_date,
            stats_season=stats_season,
            home_team_id=home_id,
            away_team_id=away_id,
            home_stats_league=home_stats_league,
            away_stats_league=away_stats_league,
        )
    except DailyLimitExceeded:
        console.print("[red]❌ API 配额耗尽，等到 UTC 00:00 后重置[/red]")
        sys.exit(1)
    except Exception as exc:
        logger.exception("预测失败: %s", exc)
        console.print(f"[red]❌ 预测失败: {exc}[/red]")
        sys.exit(1)

    # ── Step 6: 展示结果 ──
    print_data_summary(home_cn, away_cn, data_summary)
    print_prediction(home_cn, away_cn, prediction, selected_markets)

    # ── 继续？ ──
    console.print()
    if Confirm.ask("是否继续分析今日其他比赛？", default=False):
        # 重用已有的 matches 列表，避免再次查询 LLM
        if matches:
            selected2 = display_and_select_match(matches, target_date)
            if selected2:
                # 递归调用时重传原始参数会比较复杂，直接重启 main
                pass
        main()


if __name__ == "__main__":
    main()
