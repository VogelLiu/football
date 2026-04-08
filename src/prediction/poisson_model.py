"""
泊松分布进球预测模型（Dixon-Coles 风格）

原理：
  - 主队预期进球 λ_H = 联赛均值 × 主队进攻强度 × 客队防守强度 × 主场优势系数
  - 客队预期进球 λ_A = 联赛均值 × 客队进攻强度 × 主队防守强度
  - 利用泊松分布计算各比分概率矩阵，进而推导 1X2、大小球、双方进球概率
"""
import math
from typing import Optional


# 各联赛主客场平均进球数（基准值，用于强度归一化）
# 数据来源：近3赛季欧洲五大联赛统计均值
_LEAGUE_AVG_GOALS: dict[int, tuple[float, float]] = {
    2:   (1.38, 1.05),   # 欧冠
    39:  (1.53, 1.14),   # 英超
    78:  (1.70, 1.28),   # 德甲
    135: (1.48, 1.09),   # 意甲
    140: (1.52, 1.10),   # 西甲
    61:  (1.44, 1.06),   # 法甲
    169: (1.55, 1.15),   # 中超
}
_DEFAULT_LEAGUE_AVG = (1.50, 1.10)   # 通用默认值

# 主场优势系数（全球足球统计约 1.10~1.15）
_HOME_ADVANTAGE = 1.12


def _poisson_pmf(k: int, lam: float) -> float:
    """泊松分布概率质量函数 P(X=k; λ)，纯 Python 实现无需 scipy。"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _extract_per_game(stats: dict, side: str) -> tuple[float, float]:
    """
    从 API Football team_statistics 中提取指定主/客场的
    每场进球数和每场失球数。返回 (goals_for_per_game, goals_against_per_game)。
    side: 'home' 或 'away'
    """
    fixtures = stats.get("fixtures", {})
    goals = stats.get("goals", {})
    played = fixtures.get("played", {}).get(side, 0) or 0
    if played == 0:
        # 回退到全场均值
        total_played = fixtures.get("played", {}).get("total", 0) or 0
        if total_played == 0:
            return 1.30, 1.10    # 彻底无数据时给默认值
        gf = (goals.get("for", {}).get("total", {}).get("total") or 0) / total_played
        ga = (goals.get("against", {}).get("total", {}).get("total") or 0) / total_played
        return gf, ga
    gf = (goals.get("for", {}).get("total", {}).get(side) or 0) / played
    ga = (goals.get("against", {}).get("total", {}).get(side) or 0) / played
    return gf, ga


def compute_expected_goals(
    home_stats: dict,
    away_stats: dict,
    league_id: int,
) -> tuple[float, float]:
    """
    计算主队和客队的预期进球数 (λ_home, λ_away)。

    攻击/防守强度 = 球队实际均值 / 联赛均值
    主队用主场数据，客队用客场数据。
    """
    mu_h, mu_a = _LEAGUE_AVG_GOALS.get(league_id, _DEFAULT_LEAGUE_AVG)

    # 主队——用主场数据
    home_gf_pg, home_ga_pg = _extract_per_game(home_stats, "home")
    # 客队——用客场数据
    away_gf_pg, away_ga_pg = _extract_per_game(away_stats, "away")

    # 强度指数（相对联赛均值）
    home_attack  = max(home_gf_pg / mu_h, 0.20)   # 进攻强度（主场进球/联赛主场均）
    home_defense = max(home_ga_pg / mu_a, 0.20)   # 防守强度（主场失球/联赛客场均）
    away_attack  = max(away_gf_pg / mu_a, 0.20)   # 进攻强度（客场进球/联赛客场均）
    away_defense = max(away_ga_pg / mu_h, 0.20)   # 防守强度（客场失球/联赛主场均）

    lambda_home = mu_h * home_attack * away_defense * _HOME_ADVANTAGE
    lambda_away = mu_a * away_attack * home_defense

    # 合理区间限制（0.30 ~ 5.0）
    lambda_home = max(0.30, min(5.0, lambda_home))
    lambda_away = max(0.30, min(5.0, lambda_away))

    return round(lambda_home, 3), round(lambda_away, 3)


def build_score_matrix(lambda_home: float, lambda_away: float, max_goals: int = 8) -> dict:
    """
    构建比分概率矩阵 {(i, j): P(home=i, away=j)}。
    假设主客队进球相互独立（泊松过程）。
    """
    matrix = {}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            matrix[(i, j)] = _poisson_pmf(i, lambda_home) * _poisson_pmf(j, lambda_away)
    return matrix


def derive_market_probs(matrix: dict) -> dict:
    """
    从比分矩阵推导各投注市场概率。
    返回字典包含: home_win, draw, away_win, over_25, under_25, btts,
                  most_likely_score, most_likely_prob,
                  top5_scores (list of ((i,j), prob))
    """
    home_win  = sum(v for (i, j), v in matrix.items() if i > j)
    draw      = sum(v for (i, j), v in matrix.items() if i == j)
    away_win  = sum(v for (i, j), v in matrix.items() if i < j)
    over_25   = sum(v for (i, j), v in matrix.items() if i + j > 2)
    under_25  = 1.0 - over_25
    btts      = sum(v for (i, j), v in matrix.items() if i > 0 and j > 0)

    sorted_scores = sorted(matrix.items(), key=lambda x: x[1], reverse=True)
    most_likely_score, most_likely_prob = sorted_scores[0]
    top5 = sorted_scores[:5]

    return {
        "home_win":          round(home_win, 4),
        "draw":              round(draw, 4),
        "away_win":          round(away_win, 4),
        "over_25":           round(over_25, 4),
        "under_25":          round(under_25, 4),
        "btts":              round(btts, 4),
        "most_likely_score": most_likely_score,
        "most_likely_prob":  round(most_likely_prob, 4),
        "top5_scores":       top5,
    }


def format_poisson_summary(
    lambda_home: float,
    lambda_away: float,
    probs: dict,
    home_name: str,
    away_name: str,
) -> str:
    """生成面向 LLM 的泊松模型摘要文本（中文）。"""
    s = probs["most_likely_score"]
    top5_lines = []
    for (i, j), p in probs["top5_scores"]:
        top5_lines.append(f"    {i}-{j}  （{p:.1%}）")

    # 隐含赔率（1/概率）
    hw_odd  = f"{1/probs['home_win']:.2f}"  if probs["home_win"]  > 0.01 else "N/A"
    dr_odd  = f"{1/probs['draw']:.2f}"      if probs["draw"]       > 0.01 else "N/A"
    aw_odd  = f"{1/probs['away_win']:.2f}"  if probs["away_win"]   > 0.01 else "N/A"
    o25_odd = f"{1/probs['over_25']:.2f}"   if probs["over_25"]    > 0.01 else "N/A"

    return (
        f"**泊松统计模型（基于赛季进失球均值，仅供参考）**\n\n"
        f"  预期进球数：{home_name} {lambda_home:.2f} 球 vs {away_name} {lambda_away:.2f} 球\n\n"
        f"  1X2 概率：\n"
        f"    主胜 {probs['home_win']:.1%}（隐含赔率 {hw_odd}）\n"
        f"    平局 {probs['draw']:.1%}（隐含赔率 {dr_odd}）\n"
        f"    客胜 {probs['away_win']:.1%}（隐含赔率 {aw_odd}）\n\n"
        f"  大球 2.5 概率：{probs['over_25']:.1%}（隐含赔率 {o25_odd}）\n"
        f"  双方进球（BTTS）概率：{probs['btts']:.1%}\n\n"
        f"  最可能比分（Top 5）：\n"
        + "\n".join(top5_lines) + "\n\n"
        f"  ⚠️ 泊松模型仅基于历史进/失球统计，不含伤兵/战术/状态等因素，\n"
        f"     须结合其他数据综合判断。"
    )


def run_poisson_model(
    home_stats: dict,
    away_stats: dict,
    league_id: int,
    home_name: str,
    away_name: str,
) -> Optional[str]:
    """
    完整泊松模型流程：stats → 预期进球 → 比分矩阵 → 市场概率 → 格式化摘要。
    失败时返回 None。
    """
    try:
        lambda_home, lambda_away = compute_expected_goals(home_stats, away_stats, league_id)
        matrix = build_score_matrix(lambda_home, lambda_away)
        probs = derive_market_probs(matrix)
        return format_poisson_summary(lambda_home, lambda_away, probs, home_name, away_name)
    except Exception as exc:
        return None
