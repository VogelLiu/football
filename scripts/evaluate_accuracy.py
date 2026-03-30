"""
每周准确率评估 + 自动 Prompt 优化脚本。
建议每周一运行一次。

用法:
    python scripts/evaluate_accuracy.py
    python scripts/evaluate_accuracy.py --backfill 39    # 先回填英超结果再评估
    python scripts/evaluate_accuracy.py --optimize       # 评估后自动生成新 Prompt 版本
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.logger import get_logger
from src.models import init_db, check_disk_available
from src.learning import (
    backfill_results, generate_accuracy_report,
    generate_next_prompt_version, promote_prompt_if_better,
)

logger = get_logger("evaluate_accuracy")


def main() -> None:
    parser = argparse.ArgumentParser(description="准确率评估 + Prompt 自我优化")
    parser.add_argument("--backfill", type=int, metavar="LEAGUE_ID", help="先回填指定联赛的结果")
    parser.add_argument("--optimize", action="store_true", help="自动生成新 Prompt 版本")
    parser.add_argument("--promote", type=int, metavar="CANDIDATE_VERSION", help="尝试晋升候选版本")
    args = parser.parse_args()

    if not check_disk_available():
        sys.exit(1)

    init_db()

    # 1. 回填结果
    if args.backfill:
        season = 2025
        updated = backfill_results(args.backfill, season)
        logger.info("回填完成，更新 %d 条", updated)

    # 2. 打印准确率报告
    report = generate_accuracy_report()
    print("\n" + report + "\n")

    # 3. 自动生成新 Prompt 版本
    if args.optimize:
        new_v = generate_next_prompt_version(base_version=1)
        if new_v > 1:
            logger.info("新版本 v%d 已生成，运行 20+ 场后用 --promote %d 尝试晋升", new_v, new_v)

    # 4. 尝试晋升候选 Prompt
    if args.promote:
        promoted = promote_prompt_if_better(candidate_version=args.promote, baseline_version=1)
        if promoted:
            logger.info("[green]✓ v%d 已成为生产 Prompt[/green]", args.promote)
        else:
            logger.info("v%d 暂未晋升", args.promote)


if __name__ == "__main__":
    main()
