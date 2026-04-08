# ⚽ Football Prediction System

基于 **Gemini 2.5 Flash + Google Search Grounding** 的足球比赛实时分析与预测系统，覆盖欧冠、英超、西甲、德甲、意甲、法甲、中超。

---

## 功能特性

- **实时数据联网查询**：通过 Gemini Search 自动抓取伤兵/停赛、近期状态、战术风格（无需手动输入）
- **泊松模型统计基线**：基于进失球率计算主客队期望进球数（λ），输出 1X2/大小球/BTTS/最可能比分
- **多源数据融合**：API-Football 历史统计 + football-data.org 赛程 + Bet365 赔率（可选）+ H2H 记录
- **LLM 结构化预测**：Gemini 2.5 Flash 综合所有维度，输出 JSON 格式预测（1X2、让球、大小球、BTTS、推荐投注）
- **自动重试**：Gemini 503 过载时指数退避重试（最多5次），串行查询避免并发触发过载

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

### 2. 配置 `.env`

复制并填写以下必填项：

```env
# 必填 ——————————————————————————————————————————
API_FOOTBALL_KEY=your_key        # https://www.api-football.com/
FOOTBALL_DATA_KEY=your_key       # https://www.football-data.org/client/register
GEMINI_API_KEY=your_key          # https://aistudio.google.com/app/apikey
GEMINI_MODEL=gemini-2.5-flash

# 可选 ——————————————————————————————————————————
ODDS_API_KEY=your_key            # https://the-odds-api.com/（免费500次/月，获取Bet365赔率）

# 行为配置
TARGET_LEAGUES=39,140,78,135,61,169,2   # 39=英超 140=西甲 78=德甲 135=意甲 61=法甲 169=中超 2=欧冠
API_DAILY_LIMIT=100                      # API-Football 每日请求上限（免费层100次）
DB_PATH=./data/football.db
LOG_LEVEL=INFO
```

### 3. 初始化数据库

```bash
python scripts/setup_db.py
```

---

## 使用方法

### 今日赛程预测（推荐）

```bash
python scripts/today.py                      # 今天所有目标联赛
python scripts/today.py --date 2026-04-08    # 指定日期
python scripts/today.py --league 39          # 只看英超
```

交互流程：
1. 拉取当日赛程并展示列表
2. 用户选择场次
3. 自动查询：伤兵 → 近期状态 → 战术分析（串行，每步间隔3s）
4. 泊松模型计算期望进球
5. Gemini 综合分析输出预测

### 单场指定赛事预测

```bash
# 用 fixture ID 直接预测（最准确）
python scripts/predict_fixture.py --fixture-id 1234567

# 按队名+日期查找
python scripts/predict_fixture.py --home "Bayern Munich" --away "Real Madrid" --league 2 --date 2026-04-15
```

---

## 数据源说明

| 数据源 | 用途 | 免费层限制 |
|---|---|---|
| **football-data.org** | 当日赛程（欧冠+五大联赛） | 10次/分钟 |
| **API-Football** | 积分榜、球队统计、H2H、阵容 | 100次/天 |
| **Gemini 2.5 Flash** | 伤兵查询、近期状态、战术分析、LLM预测 | 无速率上限（服务器容量限制，503时自动重试）|
| **The Odds API** | Bet365 赔率（1X2 + 大小球） | 500次/月（可选） |

---

## 预测输出示例

```
╭─ 巴黎圣日耳曼 vs 利物浦 ─────────────────╮
│ 🏆 欧冠  |  主场：巴黎圣日耳曼            │
╰────────────────────────────────────────────╯

泊松模型：λ主=1.62  λ客=1.14
  主胜 44.2%  平 26.1%  客胜 29.7%
  大球(>2.5) 52.3%  BTTS 51.8%

伤兵：Kimpembe(膝盖/HIGH)...
近期状态：主队4胜1平，势头上升...

推荐投注：主胜  置信度：68%
备注：主场优势显著，客队核心后卫缺阵
```

---

## 项目结构

```
scripts/
  today.py             # 今日赛程交互预测（主入口）
  predict_fixture.py   # 单场比赛快速预测
  setup_db.py          # 初始化数据库
  evaluate_accuracy.py # 预测准确率评估
  daily_prediction.py  # 定时任务入口
  cron_example.py      # Cron 配置示例

src/
  config.py            # 全局配置（读取 .env）
  agent/
    agent_loop.py      # 数据聚合 + 预测主逻辑
    llm_providers.py   # Gemini 调用封装（重试/503处理）
    prompts.py         # 系统提示词 + 用户提示词模板
  data_collection/
    api_football.py    # API-Football 封装（限额管理）
    sources.py         # football-data.org 赛程
    scrapers.py        # Bet365 赔率（The Odds API）
  prediction/
    poisson_model.py   # 泊松分布统计模型
    output_schemas.py  # LLM 输出结构（Pydantic）
  models/
    schema.py          # SQLAlchemy 数据模型
    db_manager.py      # 数据库操作
  learning/
    feedback_loop.py   # 预测结果记录与回测
```

---

## 注意事项

**Gemini 503 过载**：`gemini-2.5-flash` 为共享容量，高峰期服务器瞬时过载会返回503。系统已处理：
- 串行执行5个搜索请求（每个间隔3s）
- 503时自动重试最多5次（间隔15s→30s→60s→120s）
- 如反复失败，等待1分钟后重新运行即可

**API-Football 配额**：免费层每天100次请求。每场比赛消耗约4-6次（积分榜×2 + 球队统计×2 + H2H×1）。`API_DAILY_LIMIT` 用尽后系统自动停止调用。
