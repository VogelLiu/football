"""
初始化数据库：建表、写入初始数据、初始化 Prompt v1。
首次运行或移动硬盘首次使用时执行此脚本。

用法:
    python scripts/setup_db.py
    python scripts/setup_db.py --db-path /Volumes/MyDisk/football_data/football.db
"""
import argparse
import sys
from pathlib import Path

# 将项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.logger import get_logger
from src.models import init_db, check_disk_available, get_db
from src.models.schema import PromptVersion
from src.agent.prompts import SYSTEM_PROMPT_V1

logger = get_logger("setup_db")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化足球预测 Agent 数据库")
    parser.add_argument("--db-path", help="覆盖 .env 中的 DB_PATH（用于移动硬盘场景）")
    args = parser.parse_args()

    if args.db_path:
        import os
        os.environ["DB_PATH"] = args.db_path
        # 重新加载 settings
        from importlib import reload
        import src.config as cfg
        reload(cfg)

    if not check_disk_available():
        sys.exit(1)

    logger.info("正在初始化数据库: %s", settings.db_path)
    init_db()

    # 写入 Prompt v1（如果不存在）
    with get_db() as db:
        existing = db.query(PromptVersion).filter_by(version=1).first()
        if not existing:
            db.add(PromptVersion(
                version=1,
                system_prompt=SYSTEM_PROMPT_V1,
                description="初始版本，人工编写的分析框架",
                is_active=True,
            ))
            db.commit()
            logger.info("Prompt v1 已写入数据库")
        else:
            logger.info("Prompt v1 已存在，跳过")

    logger.info("[green]✓ 数据库初始化完成[/green]")
    logger.info("下一步: 复制 .env.example 为 .env 并填入 API Key，然后运行 daily_prediction.py")


if __name__ == "__main__":
    main()
