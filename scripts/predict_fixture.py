"""
单场比赛快速预测脚本。
支持通过 fixture_id 或搜索队名来预测一场指定比赛。

用法:
    # 用 fixture ID 直接预测（推荐，准确）
    python scripts/predict_fixture.py --fixture-id 1234567

    # 搜索+预测（按日期+联赛+队名查找 fixture）
    python scripts/predict_fixture.py --home "Bayern Munich" --away "Real Madrid" --league 2 --date 2026-04-07
    python scripts/predict_fixture.py --home "Bayern Munich" --away "Real Madrid" --league 2
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from src.config import settings
from src.data_collection import (
    get_fixtures_by_date, search_fixtures_around_date,
    DailyLimitExceeded, get_remaining_quota,
)
from src.data_collection.api_football import _request
from src.logger import get_logger
from src.models import init_db, check_disk_available, get_db
from src.models.schema import Match, Team, PromptVersion
from src.agent.agent_loop import predict_match

logger = get_logger("predict_fixture")
console = Console()


def get_active_prompt_version(db) -> int:
    pv = db.query(PromptVersion).filter_by(is_active=True).order_by(PromptVersion.version.desc()).first()
    return pv.version if pv else 1


def upsert_team(db, team_data: dict, league_id: int) -> Team:
    api_id = team_data.get("id")
    team = db.query(Team).filter_by(api_football_id=api_id).first()
    if not team:
        team = Team(
            api_football_id=api_id,
            name=team_data.get("name", "Unknown"),
            logo_url=team_data.get("logo"),
            league_id=league_id,
        )
        db.add(team)
        db.flush()
    return team


def upsert_match_from_fixture(db, fixture: dict, league_id: int, season: int) -> Match:
    fx = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    api_id = fx.get("id")

    home_team = upsert_team(db, teams["home"], league_id)
    away_team = upsert_team(db, teams["away"], league_id)

    date_str = fx.get("date", "")
    match_date = (
        datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        if date_str else datetime.utcnow()
    )

    match = db.query(Match).filter_by(api_football_id=api_id).first()
    if not match:
        league_info = fixture.get("league", {})
        match = Match(
            api_football_id=api_id,
            league_id=league_id,
            league_name=league_info.get("name"),
            season=season,
            round=league_info.get("round"),
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            match_date=match_date,
            venue=fx.get("venue", {}).get("name"),
            status="scheduled",
        )
        db.add(match)
    else:
        match.status = fx.get("status", {}).get("short", "scheduled").lower()

    db.flush()
    match.home_team = home_team
    match.away_team = away_team
    return match


def find_fixture_by_teams(home_name: str, away_name: str, league_id: int, season: int, date_str: str | None) -> dict | None:
    """搜索联赛赛程，按队名匹配目标比赛（使用日期查询，兼容免费 API 计划）"""
    from datetime import date as date_cls, timedelta

    home_lower = home_name.lower()
    away_lower = away_name.lower()

    # 如果指定了日期，只搜那一天；否则搜今天和前后各2天
    if date_str:
        dates_to_search = [date_str]
    else:
        today = date_cls.today()
        dates_to_search = [str(today + timedelta(days=d)) for d in range(-1, 3)]

    for d in dates_to_search:
        console.print(f"[dim]搜索 {d} 的赛程 (联赛 {league_id} 赛季 {season})...[/dim]")
        try:
            fixtures = get_fixtures_by_date(league_id, season, d)
        except DailyLimitExceeded:
            console.print("[red]❌ API 配额已耗尽[/red]")
            return None

        for fx in fixtures:
            teams = fx.get("teams", {})
            h = teams.get("home", {}).get("name", "").lower()
            a = teams.get("away", {}).get("name", "").lower()
            if home_lower in h and away_lower in a:
                return fx

    return None


def print_prediction(match: Match, prediction) -> None:
    """打印完整预测结果"""
    home = match.home_team.name if match.home_team else "?"
    away = match.away_team.name if match.away_team else "?"

    p1x2 = prediction.pred_1x2 or {}
    psc = prediction.pred_score or {}
    pou = prediction.pred_ou_25 or {}
    php = prediction.pred_asian_hcp or {}
    pbtts = prediction.pred_btts or {}

    # 摘要 table
    table = Table(show_header=True, header_style="bold cyan", title=f"⚽ {home} vs {away}")
    table.add_column("市场", style="bold white", min_width=12)
    table.add_column("预测", justify="center", min_width=12)
    table.add_column("置信度", justify="center")

    table.add_row("胜平负(1X2)", p1x2.get("prediction", "?"),
                  f"{p1x2.get('confidence', 0):.1%}")
    table.add_row("比分", f"{psc.get('home','?')}-{psc.get('away','?')}",
                  f"{psc.get('confidence', 0):.1%}")
    table.add_row("大小球2.5", pou.get("side", "?"),
                  f"{pou.get('confidence', 0):.1%}")
    table.add_row("亚盘", f"{php.get('side','?')} {php.get('line','?')}",
                  f"{php.get('confidence', 0):.1%}")
    table.add_row("双方进球", pbtts.get("prediction", "?"),
                  f"{pbtts.get('confidence', 0):.1%}")

    console.print(table)

    # 推荐
    console.print(Panel(
        f"[bold yellow]最推荐市场[/bold yellow]: {prediction.recommended_market}\n"
        f"[bold green]{prediction.recommended_detail}[/bold green]",
        title="📌 推荐投注",
        border_style="yellow",
    ))

    # 分析过程
    if prediction.reasoning:
        console.print(Panel(
            prediction.reasoning,
            title="🔍 AI 分析推理",
            border_style="blue",
        ))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="单场比赛预测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 直接用 fixture ID（最准确）
  python scripts/predict_fixture.py --fixture-id 1534909

  # 指定 fixture ID，但欧冠 API 不可用时改用各自本国联赛统计
  python scripts/predict_fixture.py --fixture-id 1534909 \\
      --home-stats-league 140 --away-stats-league 78 --stats-season 2024

  # 按队名+日期搜索
  python scripts/predict_fixture.py --home "Bayern" --away "Real Madrid" \\
      --league 2 --season 2025 --date 2026-04-07
""",
    )
    parser.add_argument("--fixture-id", type=int, help="API-Football fixture ID")
    parser.add_argument("--home", type=str, help="主队英文名（按队名搜索模式）")
    parser.add_argument("--away", type=str, help="客队英文名（按队名搜索模式）")
    parser.add_argument("--league", type=int, default=2, help="联赛 ID（默认 2=欧冠）")
    parser.add_argument("--season", type=int, help="赛季年份（默认自动推算）")
    parser.add_argument("--date", type=str, help="比赛日期 YYYY-MM-DD")
    # Stats override：当比赛联赛 API 限制时，用本国联赛的数据
    parser.add_argument("--home-stats-league", type=int,
                        help="主队统计来源联赛 ID（如 140=西甲，78=德甲，39=英超）")
    parser.add_argument("--away-stats-league", type=int,
                        help="客队统计来源联赛 ID")
    parser.add_argument("--stats-season", type=int,
                        help="统计赛季（如 2024，默认同比赛赛季）")
    args = parser.parse_args()

    if not check_disk_available():
        sys.exit(1)

    init_db()

    today = datetime.utcnow()
    default_season = today.year if today.month >= 8 else today.year - 1
    season = args.season or default_season

    console.print(f"[bold]剩余 API 配额[/bold]: {get_remaining_quota()}")

    fixture: dict | None = None

    if args.fixture_id:
        from src.data_collection import get_fixture_detail
        fixture = get_fixture_detail(args.fixture_id)
        if not fixture:
            console.print(f"[red]❌ 未找到 fixture_id={args.fixture_id}[/red]")
            sys.exit(1)
        league_id = fixture.get("league", {}).get("id", args.league)
        fx_season = fixture.get("league", {}).get("season", season)
    elif args.home and args.away:
        fixture = find_fixture_by_teams(args.home, args.away, args.league, season, args.date)
        if not fixture:
            console.print(f"[red]❌ 未找到比赛: {args.home} vs {args.away}（联赛 {args.league}，赛季 {season}）[/red]")
            console.print("[yellow]提示: 用 --fixture-id 直接指定，或加 --home-stats-league / --stats-season 覆盖统计来源[/yellow]")
            sys.exit(1)
        league_id = args.league
        fx_season = season
    else:
        parser.print_help()
        sys.exit(1)

    fx_id = fixture.get("fixture", {}).get("id")
    home_name = fixture.get("teams", {}).get("home", {}).get("name", "?")
    away_name = fixture.get("teams", {}).get("away", {}).get("name", "?")
    match_date_str = fixture.get("fixture", {}).get("date", "?")
    console.print(
        f"\n[bold green]比赛[/bold green]: {home_name} vs {away_name}  |  "
        f"{match_date_str}  |  fixture_id={fx_id}"
    )

    with get_db() as db:
        prompt_version = get_active_prompt_version(db)
        match = upsert_match_from_fixture(db, fixture, league_id, fx_season)
        db.commit()

    prediction = predict_match(
        match, fx_season, prompt_version,
        home_stats_league=args.home_stats_league,
        away_stats_league=args.away_stats_league,
        stats_season=args.stats_season,
    )
    print_prediction(match, prediction)


if __name__ == "__main__":
    main()
