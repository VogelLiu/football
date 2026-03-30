"""
多源爬虫模块：BBC Sport、Sky Sports、OddsPortal。
所有爬虫遵守 rate limiting（每域 3~6 秒间隔）。
"""
import random
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from src.logger import get_logger

logger = get_logger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _get_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _polite_sleep(min_s: float = 3.0, max_s: float = 6.0) -> None:
    """礼貌爬虫延迟，避免被封"""
    time.sleep(random.uniform(min_s, max_s))


def _fetch_html(url: str) -> Optional[str]:
    """同步 HTTP 请求，失败时返回 None"""
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers=_get_headers())
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        logger.warning("爬取失败 [%s]: %s", url, exc)
        return None


# ------------------------------------------------------------------ #
#  BBC Sport
# ------------------------------------------------------------------ #
BBC_FOOTBALL_URL = "https://www.bbc.com/sport/football"


def scrape_bbc_team_news(team_name: str) -> list[dict]:
    """
    搜索 BBC Sport 指定球队的最新新闻（伤兵/阵容消息）。
    返回: [{"title": ..., "url": ..., "summary": ..., "source": "bbc-sport", "credibility": 0.85}]
    """
    query = f"{team_name} injury team news"
    search_url = f"https://www.bbc.co.uk/search?q={urlencode({'q': query})}&filter=sport"
    html = _fetch_html(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    # BBC 搜索结果结构（CSS selector 可能随重构变化，需定期维护）
    for item in soup.select("div[data-testid='default-promo']")[:5]:
        title_tag = item.select_one("p[data-testid='card-headline']")
        link_tag = item.select_one("a[data-testid='internal-link']")
        summary_tag = item.select_one("p[data-testid='card-description']")
        if not title_tag or not link_tag:
            continue
        href = link_tag.get("href", "")
        if not href.startswith("http"):
            href = "https://www.bbc.co.uk" + href
        results.append({
            "title": title_tag.get_text(strip=True),
            "url": href,
            "summary": summary_tag.get_text(strip=True) if summary_tag else "",
            "source": "bbc-sport",
            "credibility": 0.85,
            "scraped_at": datetime.utcnow().isoformat(),
        })
    _polite_sleep()
    return results


# ------------------------------------------------------------------ #
#  Sky Sports
# ------------------------------------------------------------------ #
def scrape_sky_sports_team_news(team_name: str) -> list[dict]:
    """
    爬取 Sky Sports 球队新闻（伤兵/战术分析）。
    Sky Sports 使用服务端渲染，可用 httpx 直接抓取。
    """
    slug = team_name.lower().replace(" ", "-")
    url = f"https://www.skysports.com/{slug}-news"
    html = _fetch_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.select("div.news-list__item")[:5]:
        title_tag = item.select_one(".news-list__headline")
        link_tag = item.select_one("a")
        if not title_tag or not link_tag:
            continue
        href = link_tag.get("href", "")
        if not href.startswith("http"):
            href = "https://www.skysports.com" + href
        results.append({
            "title": title_tag.get_text(strip=True),
            "url": href,
            "summary": "",
            "source": "sky-sports",
            "credibility": 0.75,
            "scraped_at": datetime.utcnow().isoformat(),
        })
    _polite_sleep()
    return results


# ------------------------------------------------------------------ #
#  OddsPortal（赔率）—— 使用 Playwright 处理 JS 动态渲染
# ------------------------------------------------------------------ #
def scrape_oddsportal_match(home_team: str, away_team: str) -> Optional[dict]:
    """
    从 OddsPortal 爬取指定比赛的赔率快照（1X2 + 大小球 + 让球）。
    使用 Playwright 无头浏览器处理 JavaScript 渲染。
    仅在赛前 48 小时内执行（避免频繁请求被封）。

    返回:
    {
        "home_win": 1.85, "draw": 3.40, "away_win": 4.20,
        "over_25": 1.72, "under_25": 2.10,
        "source": "oddsportal", "credibility": 0.75
    }
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright 未安装，请运行: pip install playwright && playwright install chromium")
        return None

    # 构建搜索 URL
    query = f"{home_team} {away_team}"
    search_url = f"https://www.oddsportal.com/search/results/?q={urlencode({'q': query})}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=30000)
            _polite_sleep(4, 7)

            # 找第一个匹配的比赛链接
            match_link = page.query_selector("a.eventRow-link")
            if not match_link:
                logger.info("OddsPortal 未找到比赛: %s vs %s", home_team, away_team)
                browser.close()
                return None

            match_url = match_link.get_attribute("href")
            if match_url and not match_url.startswith("http"):
                match_url = "https://www.oddsportal.com" + match_url

            page.goto(match_url, wait_until="networkidle", timeout=30000)
            _polite_sleep(3, 5)

            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")
        odds: dict = {"source": "oddsportal", "credibility": 0.75}

        # 解析 1X2 赔率（行结构可能随页面更新变化）
        odds_rows = soup.select("div[data-v-app] p.height-content")
        values = [r.get_text(strip=True) for r in odds_rows if r.get_text(strip=True).replace(".", "").isdigit()]
        if len(values) >= 3:
            odds["home_win"] = float(values[0])
            odds["draw"] = float(values[1])
            odds["away_win"] = float(values[2])

        return odds if len(odds) > 2 else None

    except Exception as exc:
        logger.warning("OddsPortal 爬取失败: %s", exc)
        return None


# ------------------------------------------------------------------ #
#  汇总入口
# ------------------------------------------------------------------ #
def collect_news_for_match(home_team: str, away_team: str) -> list[dict]:
    """
    聚合爬取两队的最新新闻（BBC + Sky Sports）。
    返回标注了 source 和 credibility 的新闻列表。
    """
    logger.info("爬取新闻: %s vs %s", home_team, away_team)
    news = []
    for team in [home_team, away_team]:
        news.extend(scrape_bbc_team_news(team))
        news.extend(scrape_sky_sports_team_news(team))
    return news
