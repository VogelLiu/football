"""
自学习闭环：
1. 赛后自动拉取真实结果，与预测对比记录准确率
2. 每周分析各联赛/各市场准确率，生成优化提示词
3. A/B 测试框架：新旧 Prompt 并行运行，自动晋升更优版本
"""
import json
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from src.data_collection import get_finished_results
from src.logger import get_logger
from src.models import get_db, Match, Prediction, ActualResult, PromptVersion
from src.agent.prompts import SYSTEM_PROMPT_V1

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
#  1. 结果回填
# ------------------------------------------------------------------ #
def backfill_results(league_id: int, season: int) -> int:
    """
    从 API-Football 拉取已完成比赛结果，与数据库中 pending 预测匹配并记录准确率。
    返回新更新的预测数量。
    """
    finished = get_finished_results(league_id, season, last=20)
    updated = 0

    with get_db() as db:
        for fx in finished:
            fixture = fx.get("fixture", {})
            goals = fx.get("goals", {})
            api_id = fixture.get("id")
            if not api_id:
                continue

            match = db.query(Match).filter_by(api_football_id=api_id).first()
            if not match:
                continue

            home_g = goals.get("home") or 0
            away_g = goals.get("away") or 0
            result_1x2 = "1" if home_g > away_g else ("X" if home_g == away_g else "2")

            # 更新 Match 表
            match.home_goals = home_g
            match.away_goals = away_g
            match.result_1x2 = result_1x2
            match.status = "finished"

            # 所有未评分的预测
            preds = (
                db.query(Prediction)
                .filter_by(match_id=match.id)
                .filter(~Prediction.actual_result.has())
                .all()
            )
            for pred in preds:
                ar = _evaluate_prediction(pred, home_g, away_g, result_1x2)
                db.add(ar)
                updated += 1

        db.commit()

    if updated:
        logger.info("结果回填完成，更新 %d 条预测准确率", updated)
    return updated


def _evaluate_prediction(pred: Prediction, home_g: int, away_g: int, result_1x2: str) -> ActualResult:
    """计算单条预测的各市场准确率"""
    total = home_g + away_g
    btts = home_g > 0 and away_g > 0

    # 1X2
    correct_1x2: Optional[bool] = None
    if pred.pred_1x2:
        correct_1x2 = pred.pred_1x2.get("prediction") == result_1x2

    # 比分
    correct_score: Optional[bool] = None
    if pred.pred_score:
        correct_score = (
            pred.pred_score.get("home") == home_g and pred.pred_score.get("away") == away_g
        )

    # 大小球 2.5
    correct_ou: Optional[bool] = None
    if pred.pred_ou_25:
        side = pred.pred_ou_25.get("side")
        correct_ou = (side == "over" and total > 2.5) or (side == "under" and total < 2.5)

    # 亚盘（简化：仅判断方向是否正确）
    correct_hcp: Optional[bool] = None
    if pred.pred_asian_hcp:
        hcp_side = pred.pred_asian_hcp.get("side")
        if hcp_side == "home":
            correct_hcp = result_1x2 in ("1",)
        elif hcp_side == "away":
            correct_hcp = result_1x2 in ("2",)

    # BTTS
    correct_btts: Optional[bool] = None
    if pred.pred_btts:
        pred_btts_val = pred.pred_btts.get("prediction") == "yes"
        correct_btts = pred_btts_val == btts

    # 综合得分
    results = [x for x in [correct_1x2, correct_score, correct_ou, correct_hcp, correct_btts] if x is not None]
    overall = sum(1 for x in results if x) / len(results) if results else 0.0

    return ActualResult(
        prediction_id=pred.id,
        result_1x2=result_1x2,
        home_goals=home_g,
        away_goals=away_g,
        total_goals=home_g + away_g,
        btts=btts,
        correct_1x2=correct_1x2,
        correct_score=correct_score,
        correct_ou_25=correct_ou,
        correct_asian_hcp=correct_hcp,
        correct_btts=correct_btts,
        overall_accuracy=overall,
    )


# ------------------------------------------------------------------ #
#  2. 准确率分析
# ------------------------------------------------------------------ #
def analyze_accuracy(prompt_version: Optional[int] = None) -> dict:
    """
    统计各市场的预测准确率。
    如果指定 prompt_version，只统计该版本的预测。
    """
    with get_db() as db:
        query = db.query(Prediction, ActualResult).join(
            ActualResult, ActualResult.prediction_id == Prediction.id
        )
        if prompt_version:
            query = query.filter(Prediction.prompt_version == prompt_version)

        rows = query.all()

    if not rows:
        return {"message": "暂无可分析数据", "total": 0}

    total = len(rows)
    metrics = {
        "total": total,
        "1x2_accuracy": sum(1 for _, ar in rows if ar.correct_1x2) / total,
        "score_accuracy": sum(1 for _, ar in rows if ar.correct_score) / total,
        "ou_25_accuracy": sum(1 for _, ar in rows if ar.correct_ou_25) / total,
        "asian_hcp_accuracy": sum(1 for _, ar in rows if ar.correct_asian_hcp) / total,
        "btts_accuracy": sum(1 for _, ar in rows if ar.correct_btts) / total,
        "overall_accuracy": sum(ar.overall_accuracy or 0 for _, ar in rows) / total,
    }
    return metrics


def generate_accuracy_report() -> str:
    """生成人类可读的准确率报告"""
    stats = analyze_accuracy()
    if stats.get("total", 0) == 0:
        return "暂无足够数据生成报告"

    lines = [
        f"=== 预测准确率报告（共 {stats['total']} 场）===",
        f"胜平负(1X2):  {stats['1x2_accuracy']:.1%}",
        f"比分预测:     {stats['score_accuracy']:.1%}",
        f"大小球(2.5):  {stats['ou_25_accuracy']:.1%}",
        f"亚盘让球:     {stats['asian_hcp_accuracy']:.1%}",
        f"双方进球:     {stats['btts_accuracy']:.1%}",
        f"综合得分:     {stats['overall_accuracy']:.1%}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  3. Prompt 优化 & A/B 测试
# ------------------------------------------------------------------ #
def generate_next_prompt_version(base_version: int = 1) -> int:
    """
    分析当前版本的失败案例，生成改进提示词，存入数据库，返回新版本号。
    """
    stats = analyze_accuracy(prompt_version=base_version)
    if stats.get("total", 0) < 20:
        logger.info("数据不足 20 条，暂不生成新版本（当前 %d 条）", stats.get("total", 0))
        return base_version

    # 收集失败案例
    failure_examples = _collect_failure_examples(base_version, limit=5)
    examples_text = "\n".join(
        f"- 比赛: {ex['match']}，预测: {ex['predicted']}，实际: {ex['actual']}，"
        f"失误点: {ex['failed_markets']}"
        for ex in failure_examples
    )

    weak_markets = [
        market for market, accuracy in [
            ("1X2", stats["1x2_accuracy"]),
            ("大小球", stats["ou_25_accuracy"]),
            ("亚盘", stats["asian_hcp_accuracy"]),
        ]
        if accuracy < 0.6
    ]

    addendum = f"""

## 自适应优化说明（v{base_version + 1}，基于 {stats['total']} 场历史数据）

**当前准确率**: 1X2={stats['1x2_accuracy']:.1%} | 大小球={stats['ou_25_accuracy']:.1%} | 亚盘={stats['asian_hcp_accuracy']:.1%}

**薄弱市场**: {', '.join(weak_markets) if weak_markets else '无明显薄弱项'}

**近期失败案例分析**:
{examples_text if examples_text else '暂无'}

**修正方向**:
- 预测 1X2 时，请额外关注球队近 3 场的进失球趋势（而非只看胜负）
- 亚盘预测时，优先参考主客场历史胜率差异
- 大小球预测时，综合考虑双方防守战术和主场特性
"""

    new_prompt = SYSTEM_PROMPT_V1 + addendum

    with get_db() as db:
        latest = db.query(PromptVersion).order_by(PromptVersion.version.desc()).first()
        new_version_num = (latest.version + 1) if latest else 2

        db.add(PromptVersion(
            version=new_version_num,
            system_prompt=new_prompt,
            description=f"基于 {stats['total']} 条历史数据自动优化，弱点: {weak_markets}",
            is_active=False,  # 待 A/B 测试后再激活
        ))
        db.commit()

    logger.info("新提示词版本 v%d 已生成，待 A/B 测试", new_version_num)
    return new_version_num


def _collect_failure_examples(prompt_version: int, limit: int = 5) -> list[dict]:
    with get_db() as db:
        rows = (
            db.query(Prediction, ActualResult, Match)
            .join(ActualResult, ActualResult.prediction_id == Prediction.id)
            .join(Match, Match.id == Prediction.match_id)
            .filter(Prediction.prompt_version == prompt_version)
            .filter(ActualResult.overall_accuracy < 0.4)
            .order_by(ActualResult.recorded_at.desc())
            .limit(limit)
            .all()
        )

    examples = []
    for pred, ar, match in rows:
        failed = []
        if ar.correct_1x2 is False:
            failed.append("1X2")
        if ar.correct_ou_25 is False:
            failed.append("大小球")
        if ar.correct_asian_hcp is False:
            failed.append("亚盘")
        examples.append({
            "match": f"match_id={match.id}",
            "predicted": f"1X2={pred.pred_1x2.get('prediction') if pred.pred_1x2 else '?'}",
            "actual": f"1X2={ar.result_1x2} 比分={ar.home_goals}-{ar.away_goals}",
            "failed_markets": ", ".join(failed),
        })
    return examples


def promote_prompt_if_better(candidate_version: int, baseline_version: int) -> bool:
    """
    如果候选版本在 20+ 场比赛中准确率高于基线版本，则激活候选版本。
    返回是否完成晋升。
    """
    candidate_stats = analyze_accuracy(prompt_version=candidate_version)
    baseline_stats = analyze_accuracy(prompt_version=baseline_version)

    if candidate_stats.get("total", 0) < 20:
        logger.info("候选版本 v%d 数据不足 20 场，跳过晋升", candidate_version)
        return False

    candidate_score = candidate_stats.get("overall_accuracy", 0)
    baseline_score = baseline_stats.get("overall_accuracy", 0)

    if candidate_score > baseline_score:
        with get_db() as db:
            # 取消所有旧激活
            db.query(PromptVersion).filter_by(is_active=True).update({"is_active": False})
            # 激活候选版本
            db.query(PromptVersion).filter_by(version=candidate_version).update({"is_active": True})
            db.commit()
        logger.info(
            "Prompt v%d 准确率 %.1f%% > v%d 的 %.1f%%，已晋升为生产版本",
            candidate_version, candidate_score * 100,
            baseline_version, baseline_score * 100,
        )
        return True
    else:
        logger.info(
            "候选版本 v%d (%.1f%%) 未超越基线 v%d (%.1f%%)，保持现状",
            candidate_version, candidate_score * 100,
            baseline_version, baseline_score * 100,
        )
        return False
