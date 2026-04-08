"""
Agent 主循环：为一场比赛聚合所有数据、调用 LLM 分析、保存预测结果。
"""
import sys
import uuid
from datetime import datetime
from typing import Optional

from src.agent.llm_providers import llm_provider, search_with_gemini
from src.agent.prompts import get_active_system_prompt, build_user_prompt
from src.data_collection import (
    get_team_statistics, get_head_to_head,
    get_standings, get_fixture_detail,
    DailyLimitExceeded,
)
from src.data_collection.api_football import get_coach
from src.data_collection.scrapers import fetch_bet365_odds
from src.prediction.poisson_model import run_poisson_model
from src.data_collection.sources import LEAGUE_BY_ID, TEAM_NAME_CN
from src.logger import get_logger
from src.models import get_db, Match, Prediction, SourceCredibility, MatchStatistic
from src.prediction.output_schemas import MatchPredictionOutput

logger = get_logger(__name__)

_LEAGUE_NAMES: dict[int, str] = {
    2: "欧冠", 39: "英超", 78: "德甲", 135: "意甲", 140: "西甲", 61: "法甲", 169: "中超",
}

def _league_name(league_id: int) -> str:
    return _LEAGUE_NAMES.get(league_id, f"联赛{league_id}")


# ------------------------------------------------------------------ #
#  数据聚合
# ------------------------------------------------------------------ #
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
    bookmaker = odds.get("bookmaker", "Bet365")
    return (
        f"  1X2: 主胜 {hw} / 平局 {draw} / 客胜 {aw}\n"
        f"  大小球 2.5: 大 {o25} / 小 {u25}\n"
        f"  来源: {bookmaker}（可信度 0.80）"
    )


def _format_lineup(fixture_detail: dict, team_id: int) -> str:
    """从 fixture 详情中提取首发阵容及替补"""
    lineups = fixture_detail.get("lineups", [])
    for lineup in lineups:
        if lineup.get("team", {}).get("id") == team_id:
            formation = lineup.get("formation", "未知")
            coach = lineup.get("coach", {}).get("name", "")
            start_xi = lineup.get("startXI", [])
            subs = lineup.get("substitutes", [])
            players = [p.get("player", {}).get("name", "?") for p in start_xi[:11]]
            sub_names = [p.get("player", {}).get("name", "?") for p in subs[:5]]
            lines = [f"教练: {coach}  阵型: {formation}"]
            if players:
                lines.append(f"首发: {', '.join(players)}")
            if sub_names:
                lines.append(f"替补(部分): {', '.join(sub_names)}")
            return "\n  ".join(lines)
    return "阵容未公布"


def _format_coach(coach_data: dict) -> str:
    """格式化教练信息：姓名（国籍）"""
    if not coach_data:
        return "暂无"
    name = coach_data.get("name", "未知")
    nationality = coach_data.get("nationality", "")
    age = coach_data.get("age", "")
    return f"{name}（{nationality}，{age}岁）" if nationality else name


def _extract_tactical_info(stats: dict) -> str:
    """从球队赛季统计中提取最常用阵型（coach tactics proxy）"""
    lineups_data = stats.get("lineups", [])
    if not lineups_data:
        return "暂无阵型数据"
    lineups_data = sorted(lineups_data, key=lambda x: x.get("played", 0), reverse=True)
    parts = [f"{l['formation']}（{l['played']}场）" for l in lineups_data[:3] if l.get("played", 0) > 0]
    return "、".join(parts) if parts else "暂无"


def _extract_home_away_stats(stats: dict, side: str) -> str:
    """
    提取主场（side='home'）或客场（side='away'）的详细战绩。
    返回: '主场 W胜/D平/L负 | 进X球均 Y.Yg/场 | 失X球均 Z.Zg/场'
    """
    fixtures = stats.get("fixtures", {})
    goals = stats.get("goals", {})
    played = fixtures.get("played", {}).get(side, 0) or 0
    wins = fixtures.get("wins", {}).get(side, 0) or 0
    draws = fixtures.get("draws", {}).get(side, 0) or 0
    loses = fixtures.get("loses", {}).get(side, 0) or 0
    gf = goals.get("for", {}).get("total", {}).get(side, 0) or 0
    ga = goals.get("against", {}).get("total", {}).get(side, 0) or 0
    clean_sheets = stats.get("clean_sheet", {}).get(side, 0) or 0
    label = "主场" if side == "home" else "客场"
    if played == 0:
        return f"{label}: 暂无"
    gf_avg = gf / played
    ga_avg = ga / played
    return (
        f"{label}: {wins}胜{draws}平{loses}负（{played}场）"
        f" | 进球均 {gf_avg:.1f}/场 | 失球均 {ga_avg:.1f}/场"
        f" | 零封 {clean_sheets}场"
    )


def _search_injuries_with_starter_check(team_name: str) -> str:
    """
    通过 Gemini + Google Search 搜索球队最新伤兵/停赛情况，
    并分析每位缺席球员是否为主力首发及其缺席对球队的影响程度。
    返回结构化中文文本。
    """
    from datetime import date as _d
    today_str = _d.today().strftime("%B %d, %Y")
    query = (
        f"As of {today_str}, search confirmed injury and suspension news for {team_name} football club. "
        f"For each absent player state: "
        f"(1) player name and reason (injury type or suspension reason), "
        f"(2) is this player a regular starter or a squad player? "
        f"(3) impact on team strength: HIGH (key first-XI whose absence significantly disrupts the team), "
        f"    MEDIUM (rotation player, manageable), or LOW (squad depth, negligible impact). "
        f"(4) is there a natural replacement available? "
        f"Respond entirely in Chinese. If no absences confirmed, write: 本队目前无重大伤停。"
    )
    try:
        return search_with_gemini(query).strip() or "未找到伤兵数据"
    except Exception as exc:
        logger.warning("Gemini Search 伤兵查询失败 (%s): %s", team_name, exc)
        return "伤兵信息搜索失败"


def _search_form_via_llm(team_name: str) -> str:
    """
    通过 Gemini + Google Search 搜索球队近期状态：
    最近5场比赛结果、连胜/连败情况、近期对手强度及整体势头。
    """
    from datetime import date as _d
    today_str = _d.today().strftime("%B %d, %Y")
    query = (
        f"Search the web for {team_name} football club recent form as of {today_str}. Provide: "
        f"1) Last 5 match results with date, competition name, opponent, score, and home/away. "
        f"2) Current streak (e.g. 3 wins in a row, 2 consecutive losses). "
        f"3) Opponent quality: did they recently face top-4 teams, CL sides, or relegation fodder? "
        f"4) Overall momentum: confident / struggling / inconsistent — with a brief reason. "
        f"Respond entirely in Chinese. Be concise and factual."
    )
    try:
        result = search_with_gemini(query).strip()
        return result if result else "暂无近期状态数据"
    except Exception as exc:
        logger.warning("Gemini Search 近期状态查询失败 (%s): %s", team_name, exc)
        return "近期状态搜索失败"


def _search_tactics_via_llm(home_name: str, away_name: str) -> str:
    """
    通过 Gemini + Google Search 分析两队战术风格与克制关系。
    """
    query = (
        f"Analyze the tactical matchup between {home_name} and {away_name} football clubs. "
        f"Search for their current tactical approaches and respond entirely in Chinese: "
        f"1) {home_name} style: typical formation, playing style (possession/counter-attack/high-press/low-block), key patterns. "
        f"2) {away_name} style: same. "
        f"3) Style clash: does one team's approach specifically counter the other? Be specific "
        f"   (e.g. high press vs long-ball, slow possession vs aggressive counter, deep block vs through-balls). "
        f"4) Verdict: which team holds the tactical edge in this matchup and why?"
    )
    try:
        result = search_with_gemini(query).strip()
        return result if result else "暂无战术分析数据"
    except Exception as exc:
        logger.warning("Gemini Search 战术分析失败 (%s vs %s): %s", home_name, away_name, exc)
        return "战术分析搜索失败"


def _fetch_llm_search_parallel(home_name: str, away_name: str) -> tuple:
    """
    串行执行 5 个 Gemini Search 查询，每次请求间隔 3s。

    503 UNAVAILABLE 是服务器瞬时过载，并发请求会导致所有请求同时失败并同时重试，
    形成无法打破的循环。串行执行确保每个请求独立成功后再发下一个。
    返回 (home_injuries, away_injuries, home_form_detail, away_form_detail, tactical_analysis)
    """
    import time as _time

    tasks = [
        ("主队伤兵",   _search_injuries_with_starter_check, (home_name,)),
        ("客队伤兵",   _search_injuries_with_starter_check, (away_name,)),
        ("主队近期状态", _search_form_via_llm,              (home_name,)),
        ("客队近期状态", _search_form_via_llm,              (away_name,)),
        ("战术分析",   _search_tactics_via_llm,            (home_name, away_name)),
    ]

    results = []
    total = len(tasks)
    for i, (label, fn, args) in enumerate(tasks, 1):
        print(f"  🔍 [{i}/{total}] Gemini Search: {label}...")
        results.append(fn(*args))
        if i < total:
            _time.sleep(3)  # 请求间隔 3s，避免服务器瞬时压力堆叠

    print("  ✓ 联网查询完成")
    return tuple(results)


def _search_h2h_via_llm(home_name: str, away_name: str) -> str:
    """通过 Gemini + Google Search 搜索两队历史交锋记录，返回格式化文本。"""
    query = (
        f"Search the web for head-to-head football match results between {home_name} and {away_name} "
        f"over the past 5 years. List up to 8 most recent matches with exact dates and scores. "
        f"Format each result on its own line as: YYYY-MM-DD: Home Team X-X Away Team"
    )
    try:
        text = search_with_gemini(query)
        if text and len(text.strip()) > 30:
            return text.strip()
        return ""
    except Exception as exc:
        logger.warning("Gemini Search H2H 查询失败 (%s vs %s): %s", home_name, away_name, exc)
        return ""


def gather_match_context(
    match: Match,
    season: int,
    prompt_version: int = 1,
    home_stats_league: Optional[int] = None,
    away_stats_league: Optional[int] = None,
    stats_season: Optional[int] = None,
) -> dict:
    """
    聚合一场比赛所需的所有分析数据，返回 context dict。

    home_stats_league / away_stats_league: 当比赛联赛 API 不可访问时，
        指定用哪个本国联赛 ID 来拉取球队统计（如德甲78、西甲140）。
    stats_season: 覆盖统计数据的赛季（免费 plan 只能访问 2022-2024）。
    """
    home_id = match.home_team_id
    away_id = match.away_team_id
    league_id = match.league_id
    league_cfg = LEAGUE_BY_ID.get(league_id)
    league_name = league_cfg.name_cn if league_cfg else match.league_name or str(league_id)

    home_name = match.home_team.name if match.home_team else str(home_id)
    away_name = match.away_team.name if match.away_team else str(away_id)
    home_name_cn = TEAM_NAME_CN.get(home_name, home_name)
    away_name_cn = TEAM_NAME_CN.get(away_name, away_name)

    # 统计数据用的联赛 ID / 赛季（可 override）
    home_stat_league = home_stats_league or league_id
    away_stat_league = away_stats_league or league_id
    stat_season = stats_season or season

    context: dict = {
        "league_name": league_name,
        "home_team": home_name_cn,
        "away_team": away_name_cn,
        "match_date": match.match_date.strftime("%Y-%m-%d %H:%M UTC"),
        "season": season,
        "_raw_home_stats": None,
        "_raw_away_stats": None,
    }

    # --- 联赛积分榜排名（用各自本联赛）---
    if home_id:
        try:
            home_standings = get_standings(home_stat_league, stat_season)
            rank_map_home = {s["team"]["id"]: s["rank"] for s in home_standings if "team" in s and "rank" in s}
            context["home_rank"] = f"第{rank_map_home[home_id]}名（{_league_name(home_stat_league)} {stat_season}）" if home_id in rank_map_home else "暂无"
        except (DailyLimitExceeded, Exception) as exc:
            logger.warning("获取主队积分榜失败: %s", exc)

    if away_id:
        try:
            away_standings = get_standings(away_stat_league, stat_season)
            rank_map_away = {s["team"]["id"]: s["rank"] for s in away_standings if "team" in s and "rank" in s}
            context["away_rank"] = f"第{rank_map_away[away_id]}名（{_league_name(away_stat_league)} {stat_season}）" if away_id in rank_map_away else "暂无"
        except (DailyLimitExceeded, Exception) as exc:
            logger.warning("获取客队积分榜失败: %s", exc)

    # --- 球队统计（用各自本联赛 + 可访问赛季）---
    if not home_id or not away_id:
        logger.info("部分 team_id 缺失（home=%s away=%s），跳过球队统计", home_id, away_id)
    if home_id and away_id:
        try:
            home_stats = get_team_statistics(home_id, home_stat_league, stat_season)
            away_stats = get_team_statistics(away_id, away_stat_league, stat_season)
            context["_raw_home_stats"] = home_stats
            context["_raw_away_stats"] = away_stats

            def _extract_totals(stats: dict) -> tuple:
                goals = stats.get("goals", {})
                f = goals.get("for", {}).get("total", {}).get("total", "?")
                a = goals.get("against", {}).get("total", {}).get("total", "?")
                fixtures = stats.get("fixtures", {})
                played = fixtures.get("played", {}).get("total", "-")
                wins = fixtures.get("wins", {}).get("total", "-")
                draws = fixtures.get("draws", {}).get("total", "-")
                loses = fixtures.get("loses", {}).get("total", "-")
                return f, a, f"{wins}胜{draws}平{loses}负（{played}场）"

            hgf, hga, home_total = _extract_totals(home_stats)
            agf, aga, away_total = _extract_totals(away_stats)

            context.update({
                "home_goals_for": hgf, "home_goals_against": hga,
                "away_goals_for": agf, "away_goals_against": aga,
                "home_season_record": home_total,
                "away_season_record": away_total,
            })

            # --- 泊松模型（基于赛季进/失球统计）---
            poisson_text = run_poisson_model(
                home_stats, away_stats, league_id, home_name_cn, away_name_cn
            )
            if poisson_text:
                context["poisson_analysis"] = poisson_text
                logger.info("泊松模型计算完成")

        except DailyLimitExceeded:
            logger.warning("API 配额不足，跳过球队统计")

    # --- Gemini Search 并行查询：近期状态 / 伤兵（含首发分析）/ 战术风格 ---
    (
        context["home_injuries"],
        context["away_injuries"],
        context["home_form_detail"],
        context["away_form_detail"],
        context["tactical_analysis"],
    ) = _fetch_llm_search_parallel(home_name, away_name)

    # --- 历史对阵（H2H，过滤近5年，free plan 兼容）---
    if home_id and away_id:
        from datetime import date as _date_cls
        _five_years_ago = _date_cls.today().replace(year=_date_cls.today().year - 5)

        def _fx_date(fx: dict) -> _date_cls:
            raw = fx.get("fixture", {}).get("date", "")[:10]
            try:
                return _date_cls.fromisoformat(raw)
            except ValueError:
                return _date_cls(2000, 1, 1)

        try:
            h2h_all = get_head_to_head(home_id, away_id)
            h2h = [fx for fx in h2h_all if _fx_date(fx) >= _five_years_ago]

            if h2h:
                context["head_to_head"] = _format_h2h(h2h)
                context["h2h_available"] = True
            elif h2h_all:
                # 有历史记录但超过5年
                context["head_to_head"] = (
                    f"⚠️ 近5年内两队无直接交锋记录（最近一次超过5年前）。"
                    f"\n历史对阵（供参考）:\n{_format_h2h(h2h_all[:3])}"
                )
                context["h2h_available"] = False
            else:
                logger.info("API无H2H数据，尝试 Gemini Search...")
                print(f"  🔍 Gemini Search 查询 {home_name_cn} vs {away_name_cn} 历史交锋...")
                llm_h2h = _search_h2h_via_llm(home_name, away_name)
                if llm_h2h:
                    context["head_to_head"] = f"[Gemini Search]\n{llm_h2h}"
                    context["h2h_available"] = True
                else:
                    context["head_to_head"] = "⚠️ 未找到两队历史直接对阵记录（首次交锋或数据未覆盖）。"
                    context["h2h_available"] = False
        except DailyLimitExceeded:
            logger.warning("API 配额不足，跳过历史对阵")
    else:
        # 无 team_id，直接通过 Gemini Search 查询历史对阵
        print(f"  🔍 Gemini Search 查询 {home_name_cn} vs {away_name_cn} 历史交锋...")
        llm_h2h = _search_h2h_via_llm(home_name, away_name)
        if llm_h2h:
            context["head_to_head"] = f"[Gemini Search]\n{llm_h2h}"
            context["h2h_available"] = True

    # --- 教练 & 惯用阵型（用赛季统计中的 lineups 字段）---
    if home_id and away_id:
        try:
            home_coach_data = get_coach(home_id)
            away_coach_data = get_coach(away_id)
            context["home_coach"] = _format_coach(home_coach_data)
            context["away_coach"] = _format_coach(away_coach_data)
        except DailyLimitExceeded:
            logger.warning("API 配额不足，跳过教练信息")
        except Exception as exc:
            logger.warning("获取教练信息失败: %s", exc)

    # 惯用阵型从球队统计中提取（不消耗额外配额）
    raw_home = context.get("_raw_home_stats", {})
    raw_away = context.get("_raw_away_stats", {})
    if raw_home:
        context["home_formation"] = _extract_tactical_info(raw_home)
        context["home_home_stats"] = _extract_home_away_stats(raw_home, "home")
    if raw_away:
        context["away_formation"] = _extract_tactical_info(raw_away)
        context["away_away_stats"] = _extract_home_away_stats(raw_away, "away")

    # --- Bet365 赔率（赛前 48h 才抓，需配置 ODDS_API_KEY）---
    hours_to_match = (match.match_date - datetime.utcnow()).total_seconds() / 3600
    if hours_to_match <= 48:
        odds = fetch_bet365_odds(home_name, away_name, league_id)
        context["odds_info"] = _format_odds(odds)

    # --- 平均可信度（简单估算）---
    sources_used = ["api-football"]
    if context.get("odds_info") and "暂无" not in context["odds_info"]:
        sources_used.append("bet365")
    credibility_weights = {
        "api-football": 0.95,
        "bet365": 0.80,
    }
    avg_cred = sum(credibility_weights.get(s, 0.6) for s in sources_used) / len(sources_used)
    context["_avg_credibility"] = avg_cred
    context["_sources_used"] = sources_used

    # --- 数据摘要（供展示用）---
    context["_data_summary"] = {
        "home_rank":    context.get("home_rank",    "暂无") != "暂无",
        "away_rank":    context.get("away_rank",    "暂无") != "暂无",
        "home_stats":   context.get("home_season_record", "暂无") != "暂无",
        "away_stats":   context.get("away_season_record", "暂无") != "暂无",
        "home_form":    context.get("home_form_detail", "") not in ("", "近期状态搜索失败", "暂无近期状态数据"),
        "away_form":    context.get("away_form_detail", "") not in ("", "近期状态搜索失败", "暂无近期状态数据"),
        "home_injuries": context.get("home_injuries", "") not in ("", "伤兵信息搜索失败", "未找到伤兵数据"),
        "away_injuries": context.get("away_injuries", "") not in ("", "伤兵信息搜索失败", "未找到伤兵数据"),
        "h2h":    context.get("h2h_available", False),
        "tactics": context.get("tactical_analysis", "") not in ("", "战术分析搜索失败", "暂无战术分析数据"),
        "odds":   context.get("odds_info", "暂无赔率数据") not in ("暂无赔率数据", None, ""),
    }

    return context


# ------------------------------------------------------------------ #
#  主预测函数
# ------------------------------------------------------------------ #
def predict_match(
    match: Match,
    season: int,
    prompt_version: int = 1,
    home_stats_league: Optional[int] = None,
    away_stats_league: Optional[int] = None,
    stats_season: Optional[int] = None,
) -> Prediction:
    """
    为单场比赛生成预测，保存至数据库并返回 Prediction 对象。
    home_stats_league / away_stats_league / stats_season 用于当比赛联赛
    不可访问时，改用各队本国联赛数据。
    """
    logger.info(
        "开始预测: [bold]%s vs %s[/bold] (%s)",
        match.home_team.name if match.home_team else match.home_team_id,
        match.away_team.name if match.away_team else match.away_team_id,
        match.match_date.date(),
    )

    # 1. 聚合数据
    context = gather_match_context(
        match, season, prompt_version,
        home_stats_league=home_stats_league,
        away_stats_league=away_stats_league,
        stats_season=stats_season,
    )

    # 2. 构建提示词（不含内部 _key 字段）
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
        recommended_market=result.recommended_market,
        recommended_detail=result.recommended_detail,
        prompt_version=prompt_version,
        reasoning=result.reasoning,
        avg_credibility=context.get("_avg_credibility"),
        sources_used=context.get("_sources_used"),
        llm_provider=provider,
    )

    with get_db() as db:
        db.add(prediction)

        # 持久化球队统计（本赛季汇总型，用 upsert 防重）
        home_team_db = match.home_team
        away_team_db = match.away_team
        for side, raw_stats, team_obj in [
            ("home", context.get("_raw_home_stats"), home_team_db),
            ("away", context.get("_raw_away_stats"), away_team_db),
        ]:
            if not raw_stats or not team_obj:
                continue
            existing = db.query(MatchStatistic).filter_by(
                match_id=match.id, team_id=team_obj.id
            ).first()
            if not existing:
                _stat = _parse_match_statistic(match.id, team_obj.id, raw_stats)
                if _stat:
                    db.add(_stat)

        db.commit()
        db.refresh(prediction)

    return prediction


def _parse_match_statistic(match_id: int, team_id: int, team_stats: dict) -> Optional[MatchStatistic]:
    """将 get_team_statistics() 的汇总数据转换为 MatchStatistic ORM 对象"""
    try:
        shots = team_stats.get("shots", {})
        passes = team_stats.get("passes", {})
        cards = team_stats.get("cards", {})
        return MatchStatistic(
            match_id=match_id,
            team_id=team_id,
            shots_on_goal=shots.get("on", {}).get("total"),
            shots_total=shots.get("total", {}).get("total"),
            possession=None,  # 汇总型 API 不含单场 possession
            passes_accuracy=passes.get("accuracy", {}).get("total"),
            corners=None,
            fouls=None,
            yellow_cards=_sum_card(cards.get("yellow", {})),
            red_cards=_sum_card(cards.get("red", {})),
            offsides=None,
        )
    except Exception:
        return None


def _sum_card(card_dict: dict) -> Optional[int]:
    """将 {"0-15": {"total": 2}, "16-30": {"total": 1}, ...} 合计"""
    try:
        return sum(v.get("total") or 0 for v in card_dict.values() if isinstance(v, dict))
    except Exception:
        return None


# ------------------------------------------------------------------ #
#  VirtualMatch — 不依赖数据库 ORM 的轻量比赛对象
# ------------------------------------------------------------------ #
class _TeamStub:
    """模拟 Team ORM 对象，只需 name 属性"""
    def __init__(self, name: str):
        self.name = name
        self.id = None


class VirtualMatch:
    """
    模拟 Match ORM，用于通过 LLM 发现比赛后无需写数据库即可调用
    gather_match_context() 的场景。

    api_football_id=None 时：跳过 injuries / fixture_detail 查询。
    home_team_id / away_team_id=None 时：跳过所有需要 team_id 的 API 调用。
    """
    def __init__(
        self,
        home_name: str,
        away_name: str,
        league_id: int,
        league_name: str,
        match_date: datetime,
        home_team_id: Optional[int] = None,
        away_team_id: Optional[int] = None,
        api_football_id: Optional[int] = None,
    ):
        self.home_team = _TeamStub(home_name)
        self.away_team = _TeamStub(away_name)
        self.home_team_id = home_team_id
        self.away_team_id = away_team_id
        self.league_id = league_id
        self.league_name = league_name
        self.match_date = match_date
        self.api_football_id = api_football_id
        self.id = None  # 无数据库 ID


def predict_by_team_names(
    home_name: str,
    away_name: str,
    league_id: int,
    league_name: str,
    match_date: datetime,
    stats_season: int,
    home_team_id: Optional[int] = None,
    away_team_id: Optional[int] = None,
    home_stats_league: Optional[int] = None,
    away_stats_league: Optional[int] = None,
) -> "tuple[MatchPredictionOutput, dict]":
    """
    轻量预测入口：接受球队名称 + 可选的 API team_id，
    收集历史数据后调用 LLM，直接返回 MatchPredictionOutput 而**不写数据库**。

    调用方（today.py）负责展示结果。
    team_id=None 时，大部分 API Football 统计调用会被跳过，
    LLM 仍会基于新闻/H2H/赔率等可用数据给出预测。
    """
    virtual = VirtualMatch(
        home_name=home_name,
        away_name=away_name,
        league_id=league_id,
        league_name=league_name,
        match_date=match_date,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        api_football_id=None,  # 无 fixture_id → 跳过 injuries/lineup
    )

    season = match_date.year if match_date.month >= 8 else match_date.year - 1

    context = gather_match_context(
        virtual, season,
        home_stats_league=home_stats_league or league_id,
        away_stats_league=away_stats_league or league_id,
        stats_season=stats_season,
    )

    system_prompt = get_active_system_prompt(1)
    user_prompt = build_user_prompt(context)
    result, provider = llm_provider.call_structured(system_prompt, user_prompt)
    logger.info(
        "轻量预测完成 [%s]: %s | 推荐: %s",
        provider, result.pred_1x2.prediction, result.recommended_detail,
    )
    return result, context.get("_data_summary", {})

