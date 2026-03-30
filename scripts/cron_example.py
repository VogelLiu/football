"""
cron 计划任务说明（供参考，不自动配置）

添加方法：
    crontab -e

建议计划：
  # 每天 08:00 运行每日预测（UTC+8 时区需调整）
  0 8 * * * cd /Users/liupeng/football && /usr/bin/env python scripts/daily_prediction.py >> /tmp/football_daily.log 2>&1

  # 每周一 09:00 运行准确率评估 + 自动优化
  0 9 * * 1 cd /Users/liupeng/football && /usr/bin/env python scripts/evaluate_accuracy.py --optimize >> /tmp/football_eval.log 2>&1
"""
