# Football Prediction Tool — 项目上下文文档

> **面向 LLM 的快速理解文档**。本文涵盖项目结构、数据流、已知问题及修复记录、所有可用脚本的用法。

---

## 1. 项目概述

这是一个 **足球比赛预测工具**，利用以下数据源综合分析并通过 LLM（Gemini / OpenAI）生成结构化预测结果：

| 数据源 | 类型 | 用途 |
|---|---|---|
| API-Football (RapidAPI) | REST API | 赛程、积分榜、球队统计、历史对阵、伤兵、首发阵容、教练 |
| BBC Sport | 网页爬虫 | 伤兵/赛前新闻 |
| Sky Sports | 网页爬虫 | 战术新闻 |
| OddsPortal | Playwright 无头浏览器 | 博彩赔率（1X2、大小球） |
| Google Gemini 2.5 Flash | LLM | 主分析引擎 |
| OpenAI GPT-4o | LLM | Gemini 故障切换备用 |

**目标联赛**（`TARGET_LEAGUES` 在 `.env` 配置）：
- 39 = 英超 (Premier League)
- 140 = 西甲 (La Liga)
- 78 = 德甲 (Bundesliga)
- 135 = 意甲 (Serie A)
- 61 = 法甲 (Ligue 1)
- 169 = 中超 (Chinese Super League)
- 2 = 欧冠 (Champions League，按需使用，免费 API 有限制)

---

## 2. 项目结构

```
football/
├── .env                        # 所有密钥和配置（不入 git）
├── pyproject.toml
├── requirements.txt
├── PROJECT_CONTEXT.md          # 本文件
│
├── scripts/
│   ├── today.py                # 🆕 交互式当日预测（主入口，推荐使用）
│   ├── predict_fixture.py      # 单场预测（支持 fixture_id 或队名搜索）
│   ├── daily_prediction.py     # 批量无交互自动预测（适合 cron）
│   ├── evaluate_accuracy.py    # 回测历史预测准确率
│   ├── setup_db.py             # 初始化数据库
│   └── cron_example.py         # cron 任务示例
│
├── src/
│   ├── config.py               # Settings（pydantic-settings，读 .env）
│   ├── logger.py               # rich logging
│   │
│   ├── agent/
│   │   ├── agent_loop.py       # 核心：数据聚合 + LLM 调用 + 数据库保存
│   │   ├── llm_providers.py    # Gemini/OpenAI 封装，自动故障切换
│   │   └── prompts.py          # 系统提示词 + 用户提示词模板
│   │
│   ├── data_collection/
│   │   ├── api_football.py     # API-Football 客户端（含缓存、限流、配额）
│   │   ├── scrapers.py         # BBC/Sky Sports/OddsPortal 爬虫
│   │   ├── sources.py          # 联赛配置、球队中文名映射
│   │   └── __init__.py         # 统一导出
│   │
│   ├── models/
│   │   ├── db_manager.py       # SQLAlchemy session 管理
│   │   ├── schema.py           # ORM 模型：Match, Team, Prediction, NewsItem, ...
│   │   └── __init__.py
│   │
│   ├── learning/
│   │   └── feedback_loop.py    # 回填实际结果，计算预测准确率
│   │
│   └── prediction/
│       └── output_schemas.py   # Pydantic schema：MatchPredictionOutput
│
└── data/
    ├── football.db             # SQLite 数据库
    └── cache/                  # API 响应缓存（JSON 文件，按 TTL 失效）
```

---

## 3. 数据流

```
用户输入 (today.py / predict_fixture.py)
    │
    ▼
API-Football: 查找比赛 (get_fixtures_by_date / get_fixture_detail)
    │
    ▼
gather_match_context() [agent_loop.py]
    ├── get_standings()          → 联赛积分榜排名
    ├── get_team_statistics()    → 赛季统计 + 惯用阵型（lineups 字段）
    ├── get_team_form()          → 近5场战绩（日期范围查询，免费兼容）
    ├── get_injuries()           → 伤兵名单（按 fixture_id）
    ├── get_head_to_head()       → 历史对阵（无 last 参数）
    │       └── 若空 → 自动回退到各队近5场替代
    ├── get_fixture_detail()     → 首发阵容 + 教练（赛前1小时公布）
    ├── get_coach()              → 主教练姓名、国籍
    ├── collect_news_for_match() → BBC + Sky Sports 新闻
    └── scrape_oddsportal_match()→ 赔率（仅赛前48h内执行）
    │
    ▼
build_user_prompt(context)  → 填充 Prompt 模板
    │
    ▼
LLMProvider.call_structured()
    ├── 优先 Gemini 2.5 Flash
    └── 失败 → OpenAI GPT-4o
    │
    ▼
MatchPredictionOutput (Pydantic)
    ├── pred_1x2       (胜平负)
    ├── pred_score     (比分)
    ├── pred_ou_25     (大小球)
    ├── pred_asian_hcp (亚盘让球)
    ├── pred_btts      (两队进球)
    ├── recommended_market + recommended_detail
    ├── reasoning      (LLM 分析过程，200-500字)
    └── key_factors    (3-6个关键因素)
    │
    ▼
保存至 SQLite (Match, Prediction, NewsItem, MatchStatistic)
```

---

## 4. API-Football 免费计划限制

| 限制 | 说明 |
|---|---|
| 每日请求数 | 100次/天（UTC 00:00 重置） |
| `last` 参数 | **不支持**（付费专属）|
| `next` 参数 | **不支持**（付费专属）|
| 联赛覆盖 | 欧冠 (league_id=2) 统计数据访问受限 |
| 并发 | 10次/分钟，代码内置 6秒间隔 |

**缓存 TTL（data/cache/*.json）**：
- fixtures: 24h
- standings: 12h
- team_statistics: 12h
- head_to_head: 7天
- injuries: 6h
- players/coach: 24h

---

## 5. 已发现问题 & 修复记录

### Bug #1：`last` 参数被拒绝（免费计划限制）
**报错**：`WARNING API 返回错误: {'plan': 'Free plans do not have access to the Last parameter.'}`

**受影响函数**：
- `get_team_form()` — 原用 `?team={id}&last=5`
- `get_finished_results()` — 原用 `?league=&season=&last=5&status=FT`
- `get_fixtures()` — 原用 `?league=&season=&next=10`（已有 `get_fixtures_by_date` 替代）

**修复**（`src/data_collection/api_football.py`）：
```python
# 修复后：用 from/to 日期范围 + 客户端排序取最近 N 场
def get_team_form(team_id, last=5):
    from_date = today - timedelta(days=180)
    data = _request("fixtures", {"team": team_id, "from": str(from_date),
                                  "to": str(today), "status": "FT"}, "fixtures")
    results = sorted(data["response"], key=lambda x: x["fixture"]["date"], reverse=True)
    return results[:last]
```

---

### Bug #2：Sky Sports 404（球队名含 Umlaut）
**报错**：`WARNING 爬取失败: Client error '404 Not Found' for url 'https://www.skysports.com/bayern-m%C3%BCnchen-news'`

**原因**：`München` → URL 编码为 `m%C3%BCnchen`，Sky Sports slug 不接受 Unicode。

**修复**（`src/data_collection/scrapers.py`）：
```python
import unicodedata

def _to_ascii_slug(name):
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().replace(" ", "-")
# "Bayern München" → "bayern-munchen"
```

---

### Bug #3：OddsPortal 选择器失效
**报错**：`INFO OddsPortal 未找到比赛: Real Madrid vs Bayern München`

**原因**：`a.eventRow-link` 选择器在当前 OddsPortal 页面结构中失效。

**修复**（`src/data_collection/scrapers.py`）：增加 4 级 fallback 选择器链。

---

### Bug #4：Gemini 2.0 Flash 不可用
**报错**：Gemini 调用连续失败。

**修复**：
- `.env`：`GEMINI_MODEL=gemini-2.5-flash`
- `src/config.py` 默认值同步为 `gemini-2.5-flash`

---

### 功能增强（2026-04-07）

新增以下数据字段，全部通过 API-Football 获取：

| 字段 | API 端点 | 说明 |
|---|---|---|
| `home_coach` / `away_coach` | `/coachs?team=` | 主教练姓名及国籍 |
| `home_formation` / `away_formation` | `teams/statistics.lineups` | 赛季惯用阵型频次（战术代理指标）|
| `home_home_stats` / `away_away_stats` | `teams/statistics` | 主/客场分项：胜率、进失球均值、零封数 |
| `home_form_detail` / `away_form_detail` | `fixtures?from=&to=&status=FT` | 近5场含日期+比分的详细战绩 |
| H2H 回退 | — | H2H 为空时自动用双方各自近5场替代并说明 |

---

## 6. 环境配置

### 安装

```powershell
# Windows PowerShell
cd "C:\Users\penliu\OneDrive - Capgemini\Documents\football"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # OddsPortal 爬虫需要
```

### .env 必填项

```dotenv
API_FOOTBALL_KEY=<你的 API-Football key>
GEMINI_API_KEY=<你的 Google Gemini key>
GEMINI_MODEL=gemini-2.5-flash

# 可选
OPENAI_API_KEY=<OpenAI key，Gemini 故障时启用>
```

### 初始化数据库（首次运行）

```powershell
python scripts/setup_db.py
```

---

## 7. 脚本使用方法

### 🌟 推荐日常入口：today.py（交互式）

```powershell
python scripts/today.py
```

**流程**：
1. 自动拉取今日所有目标联赛的比赛列表
2. 列出所有比赛，用户选择要分析的场次
3. 用户选择关注的博彩市场（多选）
4. 自动收集数据 → LLM 分析 → 输出结果

---

### predict_fixture.py（单场，精确）

```powershell
# 用 fixture ID（最推荐，最准确）
python scripts/predict_fixture.py --fixture-id 1534909

# 欧冠比赛，统计数据改用各自本国联赛（免费 API 不含欧冠统计）
python scripts/predict_fixture.py --fixture-id 1534909 \
    --home-stats-league 140 --away-stats-league 78 --stats-season 2024

# 按队名+日期搜索
python scripts/predict_fixture.py --home "Bayern Munich" --away "Real Madrid" \
    --league 2 --season 2025 --date 2026-04-07
```

---

### daily_prediction.py（批量自动，适合 cron）

```powershell
python scripts/daily_prediction.py              # 预测明天所有目标联赛
python scripts/daily_prediction.py --league 39  # 只预测英超
python scripts/daily_prediction.py --days-ahead 0  # 预测今天
```

---

### evaluate_accuracy.py（回测准确率）

```powershell
python scripts/evaluate_accuracy.py
```

---

## 8. 数据库模型简介

| 表 | 说明 |
|---|---|
| `teams` | 球队（api_football_id, name, logo_url, league_id）|
| `matches` | 比赛（api_football_id, league, season, 日期, 状态, 实际结果）|
| `predictions` | 预测结果（所有市场 JSON, reasoning, 置信度, llm_provider）|
| `news_items` | 爬取的新闻（source, credibility, title, content, url）|
| `match_statistics` | 球队赛季统计快照 |
| `source_credibility` | 各数据源可信度配置 |
| `prompt_versions` | 提示词版本（支持 A/B 测试）|

---

## 9. 注意事项 & 常见排错

| 问题 | 解决 |
|---|---|
| `DailyLimitExceeded` | API 配额耗尽，等到 UTC 00:00 重置 |
| `KeyError: 'league_ids'` | 检查 `.env` 中 `TARGET_LEAGUES` 格式（逗号分隔整数）|
| Gemini 调用失败 | 检查 `GEMINI_API_KEY`，确认 `GEMINI_MODEL=gemini-2.5-flash` |
| OddsPortal 无数据 | chromium 未安装：运行 `playwright install chromium` |
| 首发阵容"未公布" | 正常，赛前约1小时 API 才会有数据 |
| 欧冠统计为空 | 加 `--home-stats-league` / `--away-stats-league` 改用本国联赛 |
| 缓存数据过旧 | 删除 `data/cache/*.json` 对应文件，强制重新拉取 |

---

## 10. 关键代码入口

```python
# 核心预测调用
from src.agent.agent_loop import predict_match, gather_match_context
from src.models.schema import Match

result = predict_match(match_obj, season=2025)
# result.pred_1x2.prediction → "1" / "X" / "2"
# result.reasoning           → LLM 分析文本
# result.recommended_detail  → "主队胜，置信度72%"
```

---

*最后更新：2026-04-07*
