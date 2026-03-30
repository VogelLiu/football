"""
每日预测脚本：自动拉取明日赛程并生成预测。
建议通过 cron 每天 08:00 运行一次。

用法:
    python scripts/daily_prediction.py
    python scripts/daily_prediction.py --league 39        # 只预测英超
    python scripts/daily_prediction.py --days-ahead 2     # 预测后天比赛
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.table import Table

from src.config import settings, LEAGUE_NAMES
from src.data_collection import get_fixtures, DailyLimitExceeded, get_remaining_quota
from src.data_collection.sources import LEAGUE_BY_ID
from src.logger import get_logger
from src.models import init_db, check_disk_available, get_db
from src.models.schema import Match, Team, PromptVersion
from src.agent.agent_loop import predict_match

logger = get_logger("daily_prediction")
console = Console()


def get_active_prompt_version(db) -> int:
    pv = db.query(PromptVersion).filter_by(is_active=True).order_by(PromptVersion.version.desc()).first()
    return pv.version if pv else 1


def upsert_match(db, fixture: dict, league_id: int, season: int) -> Match:
    """将 API-Football fixture 数据写入数据库（存在则更新）"""
    fx = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    api_id = fx.get("id")

    # 确保球队记录存在
    for side in ("home", "away"):
        team_data = teams.get(side, {})
        existing_team = db.query(Team).filter_by(api_football_id=team_data.get("id")).first()
        if not existing_team:
            db.add(Team(
                api_football_id=team_data["id"],
                name=team_data.get("name", "Unknown"),
                logo_url=team_data.get("logo"),
                league_id=league_id,
            ))
    db.flush()

    # Upsert Match
    match = db.query(Match).filter_by(api_football_id=api_id).first()
    home_team = db.query(Team).filter_by(api_football_id=teams["home"]["id"]).first()
    away_team = db.query(Team).filter_by(api_football_id=teams["away"]["id"]).first()

    date_str = fx.get("date", "")
    match_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None) if date_str else datetime.utcnow()

    if not match:
        match = Match(
            api_football_id=api_id,
            league_id=league_id,
            league_name=LEAGUE_NAMES.get(league_id),
            season=season,
            round=fixture.get("league", {}).get("round"),
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
    # 手动加载关系（避免 lazy load 问题）
    match.home_team = home_team
    match.away_team = away_team
    return match


def main() -> None:
    parser = argparse.ArgumentParser(description="每日足球比赛预测")
    parser.add_argument("--league", type=int, help="只处理指定 league_id")
    parser.add_argument("--days-ahead", type=int, default=1, help="预测几天后的比赛（默认1=明天）")
    args = parser.parse_args()

    if not check_disk_available():
        sys.exit(1)

    init_db()

    target_date = (datetime.utcnow() + timedelta(days=args.days_ahead)).date()
    logger.info("目标日期: %s | 剩余 API 配额: %d", target_date, get_remaining_quota())

    league_ids = [args.league] if args.league else settings.league_ids
    season = target_date.year if target_date.month >= 8 else target_date.year - 1

    all_predictions = []

    for league_id in league_ids:
        league_cfg = LEAGUE_BY_ID.get(league_id)
        league_name = league_cfg.name_cn if league_cfg else str(league_id)

        try:
            fixtures = get_fixtures(league_id, season, next_n=10)
        except DailyLimitExceeded as e:
            logger.error("API 配额耗尽，终止: %s", e)
            break

        # 过滤目标日期的比赛
        target_fixtures = [
            fx for fx in fixtures
            if fx.get("fixture", {}).get("date", "")[:10] == str(target_date)
        ]

        if not target_fixtures:
            logger.info("[%s] %s 暂无赛程", league_name, target_date)
            continue

        logger.info("[%s] 找到 %d 场比赛，开始预测...", league_name, len(target_fixtures))

        with get_db() as db:
            prompt_version = get_active_prompt_version(db)

            for fixture in target_fixtures:
                try:
                    match = upsert_match(db, fixture, league_id, season)
                    db.commit()

                    prediction = predict_match(match, season, prompt_version)
                    all_predictions.append((match, prediction))
                except DailyLimitExceeded:
                    logger.error("API 配额耗尽，跳过剩余比赛")
                    break
                except Exception as e:
                    logger.error("预测失败 fixture=%s: %s", fixture.get("fixture", {}).get("id"), e)

    # 打印汇总表格
    if all_predictions:
        _print_summary_table(all_predictions)
    else:
        logger.info("今日无预测结果")


def _print_summary_table(predictions: list) -> None:
    table = Table(title=f"今日预测结果", show_header=True, header_style="bold cyan")
    table.add_column("比赛", style="white", min_width=30)
    table.add_column("胜平负", justify="center")
    table.add_column("比分", justify="center")
    table.add_column("大小球", justify="center")
    table.add_column("亚盘", justify="center")
    table.add_column("推荐", style="bold yellow")

    for match, pred in predictions:
        home = match.home_team.name if match.home_team else "?"
        away = match.away_team.name if match.away_team else "?"
        p1x2 = pred.pred_1x2 or {}
        psc = pred.pred_score or {}
        pou = pred.pred_ou_25 or {}
        php = pred.pred_asian_hcp or {}

        table.add_row(
            f"{home} vs {away}",
            f"{p1x2.get('prediction','?')} ({p1x2.get('confidence',0):.0%})",
            f"{psc.get('home','?')}-{psc.get('away','?')} ({psc.get('confidence',0):.0%})",
            f"{pou.get('side','?')} ({pou.get('confidence',0):.0%})",
            f"{php.get('side','?')} {php.get('line','?')} ({php.get('confidence',0):.0%})",
            pred.pred_1x2.get("prediction", "?") if pred.pred_1x2 else "?",
        )

    console.print(table)


if __name__ == "__main__":
    main()
