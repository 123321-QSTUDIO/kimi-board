#!/usr/bin/env python3
"""kimi_usage.py — 统计本机 kimi code 的 token 消耗。

数据来源：$KIMI_CODE_HOME/sessions/*/*/agents/*/wire.jsonl 中
type == "usage.record" 且 usageScope == "turn" 的记录（逐 turn 的实际用量，
不含 session 级汇总记录，避免重复计数）。

用法：
  python kimi_usage.py                # 本月（默认）
  python kimi_usage.py --hours 6      # 最近 6 小时
  python kimi_usage.py --month 2026-07
  python kimi_usage.py --all          # 全部历史
  python kimi_usage.py --hours 24 --by-workdir
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def kimi_home() -> Path:
    env = os.environ.get("KIMI_CODE_HOME")
    return Path(env) if env else Path.home() / ".kimi-code"


def month_range(month: str):
    """返回该月 [start_ms, end_ms)（本地时区）。"""
    start = datetime.strptime(month, "%Y-%m")
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def iter_usage_records(sessions_dir: Path, start_ms: int, end_ms: int):
    for wire in sessions_dir.glob("*/*/agents/*/wire.jsonl"):
        try:
            f = wire.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage.record"' not in line or '"usageScope":"turn"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "usage.record" or rec.get("usageScope") != "turn":
                    continue
                t = rec.get("time", 0)
                if not (start_ms <= t < end_ms):
                    continue
                usage = rec.get("usage") or {}
                # wire 路径: sessions/<wdKey>/<sessionId>/agents/<agent>/wire.jsonl
                yield rec.get("model", "?"), wire.parts[-5], usage, t


def fmt(n: int) -> str:
    return f"{n:,}"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="统计本机 kimi code token 消耗")
    ap.add_argument("--hours", type=float, help="最近 N 小时")
    ap.add_argument("--month", help="指定月份，格式 YYYY-MM（默认本月）")
    ap.add_argument("--all", action="store_true", help="全部历史记录")
    ap.add_argument("--by-workdir", action="store_true", help="按工作目录分组")
    args = ap.parse_args()

    now_ms = int(datetime.now().timestamp() * 1000)
    if args.all:
        start_ms, end_ms, label = 0, now_ms + 1, "全部历史"
    elif args.hours is not None:
        start_ms = now_ms - int(args.hours * 3600 * 1000)
        end_ms, label = now_ms + 1, f"最近 {args.hours:g} 小时"
    else:
        month = args.month or datetime.now().strftime("%Y-%m")
        start_ms, end_ms = month_range(month)
        label = f"{month} 月"

    sessions_dir = kimi_home() / "sessions"
    if not sessions_dir.is_dir():
        print(f"未找到会话目录：{sessions_dir}", file=sys.stderr)
        return 1

    total = defaultdict(int)
    by_model = defaultdict(lambda: defaultdict(int))
    by_wd = defaultdict(lambda: defaultdict(int))
    records = 0

    for model, wd_key, usage, _t in iter_usage_records(sessions_dir, start_ms, end_ms):
        records += 1
        for k in ("inputOther", "inputCacheRead", "inputCacheCreation", "output"):
            v = int(usage.get(k, 0))
            total[k] += v
            by_model[model][k] += v
            by_wd[wd_key][k] += v

    def report(name: str, u: dict):
        inp = u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"]
        return (
            f"{name:<28} 输入 {fmt(inp):>14} "
            f"(缓存读取 {fmt(u['inputCacheRead']):>14})  输出 {fmt(u['output']):>12}  "
            f"合计 {fmt(inp + u['output']):>14}"
        )

    print(f"kimi code token 消耗 · {label}（本机当前用户）")
    print(f"统计记录数：{records} 条 turn\n")
    print(report("【总计】", total))
    if by_model:
        print("\n按模型：")
        for model, u in sorted(by_model.items(), key=lambda kv: -sum(kv[1].values())):
            print("  " + report(model, u))
    if args.by_workdir:
        print("\n按工作目录：")
        for wd, u in sorted(by_wd.items(), key=lambda kv: -sum(kv[1].values())):
            print("  " + report(wd, u))
    return 0


if __name__ == "__main__":
    sys.exit(main())
