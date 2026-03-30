"""
Agent 主循环：为一场比赛聚合所有数据、调用 LLM 分析、保存预测结果。
"""
import uuid
from datetime import datetime
from typing import Optional

from src.agent.llm_providers import llm_provider
from src.agent.prompts import get_active_system_prompt, build_user_prompt
from src.data_collection import (
    get_team_statistics, get_injuries, get_head_to_head,
    get_team_form, collect_news_for_match, scrape_oddsportal_match,
    DailyLimitExceeded,
)
from src.data_collection.sources import LEAGUE_BY_ID, TEAM_NAME_CN
from src.logger import get_logger
from src.models import get_db, Match, Prediction, SourceCredibility
from src.prediction.output_schemas import MatchPredictionOutput

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
#  数据聚合
# ------------------------------------------------------------------ #
def _format_form(fixtures: list[dict], team_id: int) -> str:
    """将最近 N 场 fixture 转换为 W-D-L 字符串，如 'W W D L W'"""
    symbols = []
    for fx in fixtures:
        teams = fx.get("teams", {})
        goals = fx.get("goals", {})
        is_home = teams.get("home", {}).get("id") == team_id
        home_g = goals.get("home", 0) or 0
        away_g = goals.get("away", 0) or 0
        if is_home:
            result = "W" if home_g > away_g else ("D" if home_g == away_g else "L")
        else:
            result = "W" if away_g > home_g else ("D" if home_g == away_g else "L")
        symbols.append(result)
    return " ".join(symbols) if symbols else "暂无"


def _format_injuries(injuries: list[dict]) -> str:
    if not injuries:
        return "无已知伤情"
    lines = []
    for inj in injuries[:8]:
        player = inj.get("player", {})
        name = player.get("name", "未知")
        reason = inj.get("player", {}).get("reason", "未知")
        lines.append(f"  - {name}（{reason}）")
    return "\n".join(lines)


def _format_h2h(h2h_fixtures: list[dict]) -> str:
    if not h2h_fixtures:
        return "暂无历史对阵数据"
    lines = []
    for fx in h2h_fixtures[:8]:
        teams = fx.get("teams", {})
        goals = fx.get("goals", {})
        date = fx.get("fixture", {}).get("date", "?")[:10]
        home = teams.get("home", {}).get("name", "?")
        away = teams.get("away", {}).get("name", "?")
        hg = goals.get("home", "?")
        ag = goals.get("away", "?")
        lines.append(f"  {date}: {home} {hg} - {ag} {away}")
    return "\n".join(lines)


def _format_odds(odds: Optional[dict]) -> str:
    if not odds:
        return "暂无赔率数据"
    hw = odds.get("home_win", "?")
    draw = odds.get("draw", "?")
    aw = odds.get("away_win", "?")
    o25 = odds.get("over_25", "?")
    u25 = odds.get("under_25", "?")
    return (
        f"  1X2: 主胜 {hw} / 平局 {draw} / 客胜 {aw}\n"
        f"  大小球 2.5: 大 {o25} / 小 {u25}\n"
        f"  来源: OddsPortal（可信度 0.75）"
    )


def _format_news(news_items: list[dict]) -> str:
    if not news_items:
        return "暂无相关新闻"
    lines = []
    for item in news_items[:6]:
        src = item.get("source", "未知")
        cred = item.get("credibility", 0.5)
        title = item.get("title", "")
        summary = item.get("summary", "")
        text = summary if summary else title
        lines.append(f"  [{src} 可信度{cred}] {text}")
    return "\n".join(lines)


def gather_match_context(match: Match, season: int, prompt_version: int = 1) -> dict:
    """聚合一场比赛所需的所有分析数据，返回 context dict"""
    home_id = match.home_team_id
    away_id = match.away_team_id
    league_id = match.league_id
    league_cfg = LEAGUE_BY_ID.get(league_id)
    league_name = league_cfg.name_cn if league_cfg else match.league_name or str(league_id)

    home_name = match.home_team.name if match.home_team else str(home_id)
    away_name = match.away_team.name if match.away_team else str(away_id)
    home_name_cn = TEAM_NAME_CN.get(home_name, home_name)
    away_name_cn = TEAM_NAME_CN.get(away_name, away_name)

    context: dict = {
        "league_name": league_name,
        "home_team": home_name_cn,
        "away_team": away_name_cn,
        "match_date": match.match_date.strftime("%Y-%m-%d %H:%M UTC"),
        "season": season,
    }

    # 聚合各类数据（每次 API 调用前检查配额）
    try:
        home_stats = get_team_statistics(home_id, league_id, season)
        away_stats = get_team_statistics(away_id, league_id, season)

        def _extract_stats(stats: dict) -> tuple:
            goals = stats.get("goals", {})
            f = goals.get("for", {}).get("total", {}).get("total", "?")
            a = goals.get("against", {}).get("total", {}).get("total", "?")
            fixtures = stats.get("fixtures", {})
            home_rec = fixtures.get("played", {}).get("home", "?")
            wins_home = fixtures.get("wins", {}).get("home", "?")
            return f, a, f"{wins_home}胜/{home_rec}场主场"

        hgf, hga, home_rec = _extract_stats(home_stats)
        agf, aga, away_rec = _extract_stats(away_stats)

        context.update({
            "home_goals_for": hgf, "home_goals_against": hga,
            "away_goals_for": agf, "away_goals_against": aga,
            "home_home_record": home_rec, "away_away_record": away_rec,
        })
    except DailyLimitExceeded:
        logger.warning("API 配额不足，跳过球队统计")

    try:
        home_form_data = get_team_form(home_id, last=5)
        away_form_data = get_team_form(away_id, last=5)
        context["home_form"] = _format_form(home_form_data, home_id)
        context["away_form"] = _format_form(away_form_data, away_id)
    except DailyLimitExceeded:
        logger.warning("API 配额不足，跳过近期状态")

    try:
        home_injuries = get_injuries(match.api_football_id)
        context["home_injuries"] = _format_injuries(
            [i for i in home_injuries if i.get("team", {}).get("id") == home_id]
        )
        context["away_injuries"] = _format_injuries(
            [i for i in home_injuries if i.get("team", {}).get("id") == away_id]
        )
    except DailyLimitExceeded:
        logger.warning("API 配额不足，跳过伤兵信息")

    try:
        h2h = get_head_to_head(home_id, away_id, last=10)
        context["head_to_head"] = _format_h2h(h2h)
    except DailyLimitExceeded:
        logger.warning("API 配额不足，跳过历史对阵")

    # 爬虫（不消耗 API 配额）
    news = collect_news_for_match(home_name, away_name)
    context["news_summary"] = _format_news(news)

    # OddsPortal（赛前 48h 才抓）
    hours_to_match = (match.match_date - datetime.utcnow()).total_seconds() / 3600
    if hours_to_match <= 48:
        odds = scrape_oddsportal_match(home_name, away_name)
        context["odds_info"] = _format_odds(odds)

    # 平均可信度（简单估算）
    sources_used = ["api-football", "bbc-sport", "sky-sports"]
    if context.get("odds_info") and "暂无" not in context["odds_info"]:
        sources_used.append("oddsportal")
    credibility_weights = {
        "api-football": 0.95, "bbc-sport": 0.85,
        "sky-sports": 0.75, "oddsportal": 0.75,
    }
    avg_cred = sum(credibility_weights.get(s, 0.6) for s in sources_used) / len(sources_used)
    context["_avg_credibility"] = avg_cred
    context["_sources_used"] = sources_used

    return context


# ------------------------------------------------------------------ #
#  主预测函数
# ------------------------------------------------------------------ #
def predict_match(match: Match, season: int, prompt_version: int = 1) -> Prediction:
    """
    为单场比赛生成预测，保存至数据库并返回 Prediction 对象。
    """
    logger.info(
        "开始预测: [bold]%s vs %s[/bold] (%s)",
        match.home_team.name if match.home_team else match.home_team_id,
        match.away_team.name if match.away_team else match.away_team_id,
        match.match_date.date(),
    )

    # 1. 聚合数据
    context = gather_match_context(match, season, prompt_version)

    # 2. 构建提示词
    system_prompt = get_active_system_prompt(prompt_version)
    user_prompt = build_user_prompt(context)

    # 3. 调用 LLM
    result: MatchPredictionOutput
    result, provider = llm_provider.call_structured(system_prompt, user_prompt)

    logger.info(
        "预测完成 [%s]: %s | 比分 %d-%d | 大小球 %s(%.0f%%) | 推荐: %s",
        provider,
        result.pred_1x2.prediction,
        result.pred_score.home, result.pred_score.away,
        result.pred_ou_25.side, result.pred_ou_25.confidence * 100,
        result.recommended_detail,
    )

    # 4. 保存至数据库
    prediction = Prediction(
        id=str(uuid.uuid4()),
        match_id=match.id,
        pred_1x2=result.pred_1x2.model_dump(),
        pred_score=result.pred_score.model_dump(),
        pred_ou_25=result.pred_ou_25.model_dump(),
        pred_asian_hcp=result.pred_asian_hcp.model_dump(),
        pred_btts=result.pred_btts.model_dump(),
        prompt_version=prompt_version,
        reasoning=result.reasoning,
        avg_credibility=context.get("_avg_credibility"),
        sources_used=context.get("_sources_used"),
        llm_provider=provider,
    )

    with get_db() as db:
        db.add(prediction)
        db.commit()
        db.refresh(prediction)

    return prediction
