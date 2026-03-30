"""
系统提示词管理。
支持多版本，版本号与数据库 prompt_versions 表对应，供 A/B 测试使用。
"""
from src.prediction.output_schemas import MatchPredictionOutput
import json

# ------------------------------------------------------------------ #
#  系统提示词 v1（初始版本）
# ------------------------------------------------------------------ #
SYSTEM_PROMPT_V1 = """你是一位专业的足球比赛分析与预测专家，拥有丰富的欧洲五大联赛和中超联赛分析经验。

## 你的分析流程（必须按步骤推理）

**第一步：数据可信度评估**
- 对每个数据来源标注可信度权重（API官方数据=0.95，BBC/ESPN=0.80~0.85，自媒体=0.40~0.55）
- 数据冲突时，优先采信高可信度来源
- 数据不足时，在 data_quality_note 中明确说明

**第二步：主客场状态分析**
- 近5场战绩和进失球（注意主客场分开分析）
- 联赛积分榜排名及本赛季整体状态
- 球队控球风格（进攻型/防守型/平衡型）

**第三步：关键球员影响**
- 核心球员是否出战（伤病/停赛）
- 今日首发阵容（如已公布）
- 关键球员近期状态（进球/助攻/评分）

**第四步：历史对阵（H2H）**
- 近 5~10 场同场对决结果
- 本赛季已有交锋
- 历史比分模式（高比分/低比分/单方压制）

**第五步：赔率参考**
- OddsPortal 大盘赔率（可信度 0.75）
- 赔率异动（短时间内大幅变化提示内部消息）
- 主流博彩公司综合赔率

**第六步：场外因素**
- 教练战术安排（轮换/死守/主打反击）
- 赛季目标影响（争冠/保级/杯赛分心）
- 主客场氛围（主场优势/中立赛场）
- 天气/场地情况（如有影响）

**第七步：综合判断与概率估算**
- 综合以上 6 步，给出各市场的预测概率
- 明确标注哪个市场把握最大（即 recommended_market）
- 置信度 < 0.55 的市场应在推荐中提示谨慎

## 输出要求

你必须严格按照下方 JSON Schema 输出，不得添加 Schema 之外的字段：

```json
{schema}
```

- `confidence` 范围 0.0-1.0，精确到3位小数
- `reasoning` 字段写入完整的中文分析过程（200-500字）
- `key_factors` 列出3-6个关键影响因素（简短一句话）
- 若某市场数据不足，仍须给出预测但 confidence 应低于 0.55

## 重要约束

1. **不得编造数据**：若某项数据未提供，在 reasoning 中说明缺失，不可凭空捏造
2. **概率一致性**：pred_1x2 的主胜+平局+客胜置信度不需要加总为1，各市场独立置信
3. **谨慎原则**：数据置信度普遍偏低时，将所有 confidence 降至 0.5 以下
"""

# 将 JSON Schema 注入提示词
_schema_str = json.dumps(MatchPredictionOutput.model_json_schema(), ensure_ascii=False, indent=2)
SYSTEM_PROMPT_V1 = SYSTEM_PROMPT_V1.replace("{schema}", _schema_str)


# ------------------------------------------------------------------ #
#  用户侧 Prompt 模板（每场比赛填入）
# ------------------------------------------------------------------ #
USER_PROMPT_TEMPLATE = """
## 待分析比赛

**联赛**: {league_name}  
**赛事**: {home_team} vs {away_team}  
**时间**: {match_date}  
**赛季**: {season}

---

## 数据汇总

### 主队近况（{home_team}）
- 积分榜排名: {home_rank}
- 近5场: {home_form}（W-D-L）
- 本赛季进球/失球: {home_goals_for}/{home_goals_against}
- 主场战绩（本赛季）: {home_home_record}
- 伤病/停赛: {home_injuries}

### 客队近况（{away_team}）
- 积分榜排名: {away_rank}
- 近5场: {away_form}（W-D-L）
- 本赛季进球/失球: {away_goals_for}/{away_goals_against}
- 客场战绩（本赛季）: {away_away_record}
- 伤病/停赛: {away_injuries}

### 历史对阵（近10场）
{head_to_head}

### 赔率参考（OddsPortal）
{odds_info}

### 近期相关新闻
{news_summary}

---

请按步骤分析并输出完整的 JSON 预测结果。
"""


def build_user_prompt(context: dict) -> str:
    """将比赛数据字典填入 USER_PROMPT_TEMPLATE"""
    defaults = {
        "home_rank": "暂无", "away_rank": "暂无",
        "home_form": "暂无", "away_form": "暂无",
        "home_goals_for": "?", "home_goals_against": "?",
        "away_goals_for": "?", "away_goals_against": "?",
        "home_home_record": "暂无", "away_away_record": "暂无",
        "home_injuries": "无已知伤情", "away_injuries": "无已知伤情",
        "head_to_head": "暂无历史对阵数据",
        "odds_info": "暂无赔率数据",
        "news_summary": "暂无相关新闻",
    }
    merged = {**defaults, **context}
    return USER_PROMPT_TEMPLATE.format(**merged)


def get_active_system_prompt(version: int = 1) -> str:
    """根据版本号返回对应系统提示词（目前仅有 v1）"""
    prompts = {1: SYSTEM_PROMPT_V1}
    return prompts.get(version, SYSTEM_PROMPT_V1)
