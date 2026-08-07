#!/usr/bin/env python3
"""kimi_board.py — kimi code token 消耗本地网页看板（单文件、零依赖、可迁移）。

独立小服务（仅标准库），与 `kimi web` 并存，打开页面时统计一次，
页面上的"刷新"按钮重新拉取 /api/stats。
视觉：Kimi Work 看板 Hello World 风格（扁平无阴影、蓝白、六边形符号）。
费用：按 Kimi 开放平台刊例价估算（缓存命中/未命中/输出分别计价）。

数据来源：$KIMI_CODE_HOME/sessions（默认 ~/.kimi-code/sessions）下各会话
wire.jsonl 中 usageScope=="turn" 的 usage.record，含子 agent，不含 session
级汇总记录（避免重复计数）。

用法：
  python kimi_board.py                      # 默认 127.0.0.1:8321
  python kimi_board.py --port 9000 --plan-price 199 --no-open
"""

import argparse
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def kimi_home() -> Path:
    """kimi code 数据根目录，尊重 KIMI_CODE_HOME 环境变量。"""
    env = os.environ.get("KIMI_CODE_HOME")
    return Path(env) if env else Path.home() / ".kimi-code"

USAGE_KEYS = ("inputOther", "inputCacheRead", "inputCacheCreation", "output")

# Kimi 开放平台刊例价（元 / 1M tokens）：(缓存命中, 输入未命中, 输出)
# https://platform.kimi.com/docs/pricing/chat
PRICING = {
    "kimi-code/k3": (2.0, 20.0, 100.0),                     # kimi-k3
    "kimi-code/k3-256k": (2.0, 20.0, 100.0),                # k3 的 256k 变体，按 k3 价
    "kimi-code/kimi-for-coding-highspeed": (2.6, 13.0, 54.0),  # kimi-k2.7-code-highspeed
    "kimi-code/kimi-for-coding": (1.3, 6.5, 27.0),          # kimi-k2.7-code
}
DEFAULT_PRICE = (2.0, 20.0, 100.0)


def price_of(model: str):
    """查模型单价；容忍日志里不带 kimi-code/ 前缀的写法。"""
    return PRICING.get(model) or PRICING.get(f"kimi-code/{model}", DEFAULT_PRICE)

# Kimi 会员档位 -> 月付价格（元）
# https://www.kimi.com/zh-cn/resources/kimi-k3-pricing
PLAN_PRICES = {
    "adagio": 0.0,
    "andante": 49.0,
    "moderato": 99.0,
    "allegretto": 199.0,
    "allegro": 699.0,
}

PLAN_PRICE = None  # --plan-price 显式指定时优先于自动识别

_plan_cache = {"at": 0.0, "result": None}


def detect_plan():
    """从正在运行的 kimi web 实例读取会员档位，返回 (价格, 档位名)；失败返回 None。

    原理：kimi web 每个实例在 server/instances/ 注册 host/port，
    用 server.token 作为 bearer 调 /api/v1/oauth/userinfo 拿 userLevelName。
    成功结果缓存 10 分钟；失败只缓存 30 秒（避免 WebUI 短暂掉线后长时间回退默认价）。
    """
    import time
    import urllib.request

    now = time.time()
    ttl = 600 if _plan_cache["result"] else 30
    if _plan_cache["at"] and now - _plan_cache["at"] < ttl:
        return _plan_cache["result"]
    result = None
    try:
        token = (kimi_home() / "server.token").read_text(encoding="utf-8").strip()
        instances = []
        for p in (kimi_home() / "server" / "instances").glob("*.json"):
            try:
                instances.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        instances.sort(key=lambda i: -i.get("heartbeat_at", 0))
        for inst in instances[:3]:
            try:
                url = f"http://{inst.get('host', '127.0.0.1')}:{inst['port']}/api/v1/oauth/userinfo"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    data = json.loads(resp.read())
                name = ((data.get("data") or {}).get("userInfo") or {}).get("userLevelName")
                price = PLAN_PRICES.get(name.lower()) if name else None
                if price is not None:
                    result = (price, name)
                    break
            except Exception:
                continue
    except Exception:
        pass
    _plan_cache.update(at=now, result=result)
    return result


VERSION = "1.3.0"  # 与最新 release 标签（去 v 前缀）保持一致，发版时更新
GITHUB_REPO = "Pierre1231/kimi-board"

_release_cache = {"at": 0.0, "result": None, "checking": False}


def _ver_tuple(s: str):
    import re

    parts = [int(x) for x in re.findall(r"\d+", s)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def _fetch_latest_release():
    """经 /releases/latest 重定向取最新标签（网页端点，不消耗 API 配额）。
    返回 (标签号, release 页 URL)；失败返回 None。"""
    import re
    import urllib.request

    try:
        url = f"https://github.com/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "kimi-board"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            final = resp.geturl()
        m = re.search(r"/releases/tag/v?([^/]+)$", final)
        if m:
            return m.group(1), final
    except Exception:
        pass
    return None


def check_update():
    """返回 {"current", "latest", "url", "newer"}；后台线程异步刷新缓存，
    请求本身不阻塞。成功缓存 6 小时，失败缓存 10 分钟。"""
    import threading
    import time

    now = time.time()
    cached = _release_cache["result"]
    ttl = 6 * 3600 if cached and cached["latest"] else 600
    if _release_cache["at"] and now - _release_cache["at"] < ttl:
        return cached
    if not _release_cache["checking"]:
        _release_cache["checking"] = True

        def _run():
            try:
                res = {"current": VERSION, "latest": None, "url": None, "newer": False}
                r = _fetch_latest_release()
                if r:
                    latest, url = r
                    res.update(latest=latest, url=url,
                               newer=_ver_tuple(latest) > _ver_tuple(VERSION))
                _release_cache.update(at=time.time(), result=res)
            finally:
                _release_cache["checking"] = False

        threading.Thread(target=_run, daemon=True).start()
    return cached or {"current": VERSION, "latest": None, "url": None, "newer": False}

# ---------------------------------------------------------------- 数据层

_file_cache = {}  # path -> (mtime, size, [record])


def scan_wire_file(wire: Path):
    """读取一个 wire.jsonl，提取 turn 级 usage.record；按 (mtime, size) 缓存。"""
    try:
        st = wire.stat()
    except OSError:
        return []
    key = str(wire)
    cached = _file_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    records = []
    try:
        with wire.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage.record"' not in line or '"usageScope":"turn"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "usage.record" or rec.get("usageScope") != "turn":
                    continue
                # sessions/<wdKey>/<sessionId>/agents/<agent>/wire.jsonl
                records.append(
                    (
                        int(rec.get("time", 0)),
                        rec.get("model", "?"),
                        wire.parts[-4],  # sessionId
                        {k: int((rec.get("usage") or {}).get(k, 0)) for k in USAGE_KEYS},
                    )
                )
    except OSError:
        pass
    _file_cache[key] = (st.st_mtime, st.st_size, records)
    return records


def empty_usage():
    return {k: 0 for k in USAGE_KEYS}


def cost_of(model: str, u: dict) -> float:
    """按刊例价估算费用（元）。inputCacheCreation 按未命中输入计。"""
    hit, miss, out = price_of(model)
    return (
        u["inputCacheRead"] * hit
        + (u["inputOther"] + u["inputCacheCreation"]) * miss
        + u["output"] * out
    ) / 1e6


def collect_stats():
    home = kimi_home()
    sessions_dir = home / "sessions"
    now = datetime.now()
    now_ms = int(now.timestamp() * 1000)

    month_start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start = int(month_start_dt.timestamp() * 1000)
    prev_end_dt = month_start_dt
    prev_start_dt = (month_start_dt.replace(day=1) ).replace(
        year=month_start_dt.year if month_start_dt.month > 1 else month_start_dt.year - 1,
        month=month_start_dt.month - 1 if month_start_dt.month > 1 else 12,
    )
    prev_start, prev_end = int(prev_start_dt.timestamp() * 1000), int(prev_end_dt.timestamp() * 1000)
    today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    hour_start = now_ms - 3600 * 1000

    hour0 = now.replace(minute=0, second=0, microsecond=0)
    hour_bucket0 = int(hour0.timestamp() * 1000) - 23 * 3600 * 1000
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_bucket0 = int(day0.timestamp() * 1000) - 30 * 86400 * 1000

    cards = {"month": empty_usage(), "today": empty_usage(), "hour": empty_usage()}
    hourly = [empty_usage() for _ in range(24)]
    daily = [empty_usage() for _ in range(31)]
    hourly_models = [defaultdict(empty_usage) for _ in range(24)]
    daily_models = [defaultdict(empty_usage) for _ in range(31)]
    models_month = defaultdict(lambda: empty_usage())
    models_prev = defaultdict(lambda: empty_usage())
    turns = 0

    if sessions_dir.is_dir():
        for wire in sessions_dir.glob("*/*/agents/*/wire.jsonl"):
            for t, model, _sid, usage in scan_wire_file(wire):
                turns += 1

                def add(bucket):
                    for k in USAGE_KEYS:
                        bucket[k] += usage[k]

                if t >= month_start:
                    add(cards["month"])
                    add(models_month[model])
                elif prev_start <= t < prev_end:
                    add(models_prev[model])
                if t >= today_start:
                    add(cards["today"])
                if t >= hour_start:
                    add(cards["hour"])
                if t >= hour_bucket0:
                    i = (t - hour_bucket0) // (3600 * 1000)
                    add(hourly[i])
                    add(hourly_models[i][model])
                if t >= day_bucket0:
                    i = (t - day_bucket0) // (86400 * 1000)
                    add(daily[i])
                    add(daily_models[i][model])

    def finalize(u):
        inp = u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"]
        return {
            "input": inp,
            "cacheRead": u["inputCacheRead"],
            "output": u["output"],
            "total": inp + u["output"],
        }

    # ---- 费用估算 ----
    cost_by_model = []
    month_cost = cache_cost = miss_cost = out_cost = 0.0
    for model, u in models_month.items():
        hit, miss, out = price_of(model)
        cc = u["inputCacheRead"] * hit / 1e6
        mc = (u["inputOther"] + u["inputCacheCreation"]) * miss / 1e6
        oc = u["output"] * out / 1e6
        cache_cost += cc
        miss_cost += mc
        out_cost += oc
        cost_by_model.append({"name": model, "cost": round(cc + mc + oc, 2), **finalize(u)})
    prev_cost = sum(cost_of(m, u) for m, u in models_prev.items())
    # 合并上月有消耗但本月未用的模型，保证每个模型都能看到费用
    for model, u in models_prev.items():
        pc = round(cost_of(model, u), 2)
        row = next((r for r in cost_by_model if r["name"] == model), None)
        if row:
            row["prevCost"] = pc
        else:
            cost_by_model.append({"name": model, "cost": 0.0, **finalize(empty_usage()), "prevCost": pc})
    for row in cost_by_model:
        row.setdefault("prevCost", 0.0)
    cost_by_model.sort(key=lambda r: -(r["cost"] + r["prevCost"]))
    month_cost = cache_cost + miss_cost + out_cost

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    pace = month_cost / now.day * days_in_month

    # 套餐价：--plan-price 显式指定 > 自动识别（kimi web 在线时）> 默认 199
    plan_auto = detect_plan() if PLAN_PRICE is None else None
    if PLAN_PRICE is not None:
        plan_price, plan_name, plan_is_auto = PLAN_PRICE, None, False
    elif plan_auto:
        plan_price, plan_name, plan_is_auto = plan_auto[0], plan_auto[1], True
    else:
        plan_price, plan_name, plan_is_auto = 199.0, None, False
    payback_pct = round(month_cost / plan_price * 100, 1) if plan_price > 0 else None

    return {
        "generatedAt": now_ms,
        "turns": turns,
        "cards": {k: finalize(v) for k, v in cards.items()},
        "cost": {
            "monthTotal": round(month_cost, 2),
            "components": {
                "cache": round(cache_cost, 2),
                "miss": round(miss_cost, 2),
                "out": round(out_cost, 2),
            },
            "byModel": cost_by_model,
            "prevMonthLabel": prev_start_dt.strftime("%Y-%m"),
            "prevMonthTotal": round(prev_cost, 2),
            "planPrice": plan_price,
            "planName": plan_name,
            "planAuto": plan_is_auto,
            "paybackPct": payback_pct,
            "pace": round(pace, 2),
            "daysElapsed": now.day,
            "daysInMonth": days_in_month,
        },
        "hourly": [
            {"t": hour_bucket0 + i * 3600 * 1000, **finalize(u),
             "models": {m: v for m, mu in hourly_models[i].items()
                        if (v := finalize(mu)["total"]) > 0}}
            for i, u in enumerate(hourly)
        ],
        "daily": [
            {"t": day_bucket0 + i * 86400 * 1000, **finalize(u),
             "models": {m: v for m, mu in daily_models[i].items()
                        if (v := finalize(mu)["total"]) > 0}}
            for i, u in enumerate(daily)
        ],
    }


# ---------------------------------------------------------------- 页面

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kimi Code 用量看板</title>
<style>
  :root {
    --bg: #f5f8fd;
    --card: #ffffff;
    --tint: #dcebff;
    --line: #e3e9f4;
    --text: #101828;
    --dim: #5d6b82;
    --faint: #a8b4cc;
    --blue: #3a8dff;
    --blue-deep: #2e6fe8;
    --ink: #0c0e12;
    --mono: ui-monospace, "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
    --num: "Bahnschrift", "DIN Alternate", "Segoe UI", sans-serif;
    --sans: "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; }
  html { background: var(--bg); }
  body {
    background: transparent; color: var(--text);
    font-family: var(--sans); padding: 26px 32px 48px;
    max-width: 1420px; margin: 0 auto;
  }

  /* ---- 粒子背景 ---- */
  #particles { position: fixed; inset: 0; z-index: -1; pointer-events: none; }

  /* ---- 进场动效 ---- */
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .reveal { animation: rise .32s cubic-bezier(.22,.8,.36,1) both; }

  /* ---- 顶栏 ---- */
  .topbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 30px; }
  .hex { color: var(--blue); font-size: 18px; }
  .crumb { font-size: 13px; color: var(--dim); }
  .crumb b { color: var(--text); font-weight: 600; }
  .spacer { flex: 1; }
  .live-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--blue); margin-right: 7px;
    animation: pulse 2.2s ease-in-out infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
  #updated { font-size: 12px; color: var(--faint); font-family: var(--mono); }
  #refresh {
    font-size: 13px; font-weight: 550; color: #fff; border: none; cursor: pointer;
    background: var(--blue); border-radius: 10px; padding: 8px 18px;
    transition: background .15s ease, transform .1s ease;
  }
  #refresh:hover { background: var(--blue-deep); }
  #refresh:active { transform: scale(.96); }
  .update-tip {
    color: var(--blue-deep); text-decoration: none; margin-left: 10px;
    letter-spacing: inherit;
  }
  .update-tip:hover { text-decoration: underline; }

  /* ---- 主信息行 ---- */
  .hero { position: relative; margin-bottom: 22px; padding: 2px 0; }
  .hero .meta {
    min-height: 15px;
    font-family: var(--mono); font-size: 11px; letter-spacing: .2em;
    color: var(--dim); text-transform: uppercase;
  }
  .hero .meta .hex { font-size: 10px; margin-right: 8px; }
  .glyph-strip {
    position: absolute; right: 0; top: 50%; transform: translateY(-50%);
    font-family: var(--mono); font-size: 14px; letter-spacing: .3em; white-space: nowrap;
    color: var(--blue); opacity: .5; user-select: none;
    -webkit-mask-image: linear-gradient(90deg, transparent, #000 65%);
    mask-image: linear-gradient(90deg, transparent, #000 65%);
  }
  @media (max-width: 860px) {
    .glyph-strip { display: none; }
  }

  /* ---- 卡片通用 ---- */
  .card, section, .blackcard {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 22px 24px;
  }
  .card, section { transition: border-color .18s ease; }
  .card:hover, section:hover { border-color: #c9dfff; }
  .card {
    position: relative; overflow: hidden;
    background: linear-gradient(180deg, #ffffff 0%, #f6f9ff 100%);
  }
  .card::before {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background-image: radial-gradient(circle, rgba(58,141,255,.30) 1.2px, transparent 1.3px);
    background-size: 13px 13px;
    -webkit-mask-image: linear-gradient(to bottom left, #000 0%, transparent 48%);
    mask-image: linear-gradient(to bottom left, #000 0%, transparent 48%);
  }
  .card > * { position: relative; z-index: 1; }
  .caption {
    margin-top: 16px; font-family: var(--mono); font-size: 10px;
    letter-spacing: .3em; color: var(--faint); text-transform: uppercase;
  }
  .card .label {
    font-size: 15px; color: var(--text); font-weight: 650;
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  }
  .card .label .hex { font-size: 13px; }
  .card .value { font-family: var(--num); font-size: 46px; font-weight: 700; letter-spacing: .01em; font-variant-numeric: tabular-nums; margin-bottom: 12px; }
  .card .sub { font-size: 13.5px; color: var(--dim); line-height: 2.0; }
  .card .sub .row { display: flex; justify-content: space-between; gap: 12px; }
  .card .sub .num { font-family: var(--mono); color: var(--text); font-variant-numeric: tabular-nums; }
  .pill {
    display: inline-block; margin-top: 10px; font-size: 12px; font-weight: 600;
    color: var(--blue-deep); background: rgba(58,141,255,.12);
    border-radius: 999px; padding: 3px 12px; font-family: var(--mono);
  }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card.tint {
    background: linear-gradient(160deg, #eef5ff 0%, #d6e7ff 100%);
    border-color: #c9dfff;
  }
  .card.tint::before {
    background-image: radial-gradient(circle, rgba(46,111,232,.40) 1.2px, transparent 1.3px);
  }
  .card.tint .caption { color: #7fa3d8; }

  /* ---- 黑色费用卡 ---- */
  .blackcard {
    background: linear-gradient(150deg, #141a28 0%, #0a0c11 70%);
    border-color: #0a0c11; color: #fff; margin-bottom: 16px;
    position: relative; overflow: hidden;
  }
  .cost-left { position: relative; }
  .cost-left > *:not(.glyph-field) { position: relative; z-index: 1; }
  .glyph-field {
    position: absolute; left: 185px; top: 4px; right: 14px; height: 92px;
    overflow: hidden; pointer-events: none; user-select: none; z-index: 0;
    -webkit-mask-image: linear-gradient(90deg, transparent 0%, #000 15%, #000 97%, transparent 100%);
    mask-image: linear-gradient(90deg, transparent 0%, #000 15%, #000 97%, transparent 100%);
  }
  .glyph-inner {
    font-family: var(--mono); font-size: 13px; line-height: 1.75; letter-spacing: .28em;
    white-space: pre; color: #fff; height: 100%;
    -webkit-mask-image: linear-gradient(180deg, #000 55%, transparent 100%);
    mask-image: linear-gradient(180deg, #000 55%, transparent 100%);
  }
  .blackcard .cost-grid, .blackcard .model-cost, .blackcard .caption { position: relative; z-index: 1; }
  @media (max-width: 860px) { .glyph-field { display: none; } }
  .blackcard .label { font-size: 15px; color: #e8edf5; font-weight: 650; display: flex; align-items: center; gap: 8px; margin-bottom: 12px; z-index: 3; }
  .blackcard .help {
    position: relative;
    display: inline-flex; align-items: center; justify-content: center;
    color: #8b96a8; cursor: help;
    transition: color .15s ease;
  }
  .blackcard .help svg { width: 13px; height: 13px; display: block; }
  .blackcard .help:hover { color: #fff; }
  .blackcard .help .tip {
    position: absolute; left: -9px; top: 22px; z-index: 20;
    width: max-content; max-width: min(320px, 72vw);
    background: #1b2233; border: 1px solid #31405e; border-radius: 10px;
    padding: 11px 14px 10px; font-size: 11.5px; font-weight: 400; line-height: 1.6; color: #8f9ab0;
    opacity: 0; visibility: hidden; transform: translateY(-4px);
    transition: opacity .15s ease, transform .15s ease, visibility .15s;
    pointer-events: none;
  }
  .blackcard .help:hover .tip { opacity: 1; visibility: visible; transform: translateY(0); }
  .blackcard .help .tp-f { display: block; margin-bottom: 8px; }
  .blackcard .help .tp-f b { color: #dbe3ef; font-weight: 500; }
  .blackcard .help .tp-t { display: block; border-top: 1px solid #2a3450; padding: 5px 0 4px; }
  .blackcard .help .tp-r {
    display: grid; grid-template-columns: 1fr 42px 42px 46px; align-items: baseline;
    line-height: 1.9;
  }
  .blackcard .help .tp-r b { color: #dbe3ef; font-weight: 500; font-size: 11px; }
  .blackcard .help .tp-r i {
    font-family: var(--mono); font-style: normal; font-size: 11px; text-align: right;
    color: #fff; font-variant-numeric: tabular-nums;
  }
  .blackcard .help .tp-r.tp-h i { color: #5d6a85; font-size: 10px; }
  .blackcard .help .tp-note { display: block; border-top: 1px solid #2a3450; padding-top: 6px; font-size: 10.5px; color: #5d6a85; }
  .blackcard .big { font-family: var(--num); font-size: 50px; font-weight: 700; letter-spacing: .01em; font-variant-numeric: tabular-nums; margin-bottom: 14px; }
  .blackcard .sub { font-size: 13.5px; color: #97a3b6; line-height: 2.0; }
  .blackcard .sub .row { display: flex; justify-content: space-between; gap: 12px; }
  .blackcard .sub .num { font-family: var(--mono); color: #fff; font-variant-numeric: tabular-nums; }
  .blackcard .caption { color: #4b5261; }
  .cost-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 22px; }
  @media (max-width: 860px) { .cost-grid { grid-template-columns: 1fr; } }
  .payback {
    background: linear-gradient(135deg, #4c9bff 0%, #2e6fe8 100%);
    border-radius: 14px; padding: 18px 20px; color: #fff;
    display: flex; flex-direction: column; justify-content: center; gap: 8px;
    position: relative; overflow: hidden;
  }
  .payback::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background-image: radial-gradient(circle, rgba(255,255,255,.32) 1.2px, transparent 1.3px);
    background-size: 12px 12px;
    -webkit-mask-image: linear-gradient(to top left, #000 0%, transparent 55%);
    mask-image: linear-gradient(to top left, #000 0%, transparent 55%);
  }
  .payback > * { position: relative; z-index: 1; }
  .payback .pb-label { font-size: 13.5px; opacity: .85; }
  .payback .pb-value { font-family: var(--num); font-size: 36px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .payback .pb-bar { height: 8px; background: rgba(255,255,255,.3); border-radius: 999px; overflow: hidden; }
  .payback .pb-fill { height: 100%; background: #fff; border-radius: 999px; }
  .payback .pb-note { font-size: 12.5px; opacity: .85; }
  .model-cost { margin-top: 12px; border-top: 1px solid #232733; padding-top: 10px; }
  .model-cost .mc-legend { font-size: 11px; color: #6b7385; margin-bottom: 6px; font-family: var(--mono); }
  .model-cost .mc-legend .sq1 { color: var(--blue); }
  .model-cost .mc-legend .sq2 { color: #3a4152; }
  .model-cost .bar-row { margin: 6px 0; }
  .model-cost .bar-row .name { color: #9aa4b8; }
  .model-cost .bar-row .track {
    background: transparent; display: flex; flex-direction: column;
    gap: 2px; height: auto; overflow: visible; border-radius: 0;
  }
  .model-cost .bar-row .fill { background: var(--blue); height: 7px; }
  .model-cost .bar-row .fill.prev { background: #3a4152; height: 4px; }
  .model-cost .bar-row .num {
    color: #fff; width: 130px; display: flex; flex-direction: column;
    align-items: flex-end; line-height: 1.45;
  }
  .model-cost .prev-num { font-size: 10px; color: #6b7385; }

  /* ---- 区块 ---- */
  section { margin-bottom: 16px; }
  .sec-head { font-size: 16px; font-weight: 700; margin-bottom: 14px; display: flex; align-items: baseline; gap: 10px; }
  .sec-head .right { margin-left: auto; font-size: 11px; color: var(--faint); font-family: var(--mono); font-weight: 400; letter-spacing: .1em; }

  /* ---- 趋势图:范围切换 + 汇总条(DeepSeek 式,融入白卡体系) ---- */
  .trendcard { position: relative; }
  .seg { display: inline-flex; position: relative; background: #eef1f6; border-radius: 999px; padding: 3px; gap: 2px; letter-spacing: 0; }
  .seg .seg-pill {
    position: absolute; top: 3px; bottom: 3px; left: 3px; width: 0;
    background: #fff; border-radius: 999px; z-index: 0;
    transition: left .25s cubic-bezier(.22,.8,.36,1), width .25s cubic-bezier(.22,.8,.36,1);
  }
  .seg button {
    position: relative; z-index: 1;
    border: 0; background: transparent; cursor: pointer; font-family: inherit;
    font-size: 12.5px; line-height: 1; color: var(--dim); border-radius: 999px; padding: 6px 13px;
    transition: color .15s ease;
  }
  .seg button:hover { color: var(--text); }
  .seg button.on { color: var(--blue-deep); font-weight: 600; }
  .trend-sum { display: flex; gap: 14px; flex-wrap: wrap; margin: -2px 0 12px; }
  .trend-sum .ts {
    background: #f5f7fb; border-radius: 12px; padding: 10px 18px 12px;
    display: flex; flex-direction: column; gap: 4px; width: 190px; flex: 0 0 auto;
  }
  .trend-sum .ts .lb { font-size: 12.5px; color: var(--dim); }
  .trend-sum .ts .vl { display: flex; align-items: baseline; gap: 7px; width: 100%; white-space: nowrap; }
  .trend-sum .ts b { font-family: var(--num); font-size: 22px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
  .trend-sum .ts .unit { font-family: var(--mono); font-size: 11px; color: var(--faint); margin-left: auto; }
  .trend-legend { display: flex; gap: 18px; flex-wrap: wrap; margin: 0 0 6px; font-size: 12px; color: var(--dim); }
  .trend-legend .lg { display: inline-flex; align-items: baseline; gap: 7px; }
  .trend-legend .lg .sq { align-self: center; }
  .trend-legend .sq { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .trend-legend .amt { font-family: var(--mono); color: var(--faint); font-size: 11px; }

  /* 悬浮平滑过渡 */
  #trend rect.bar { transition: opacity .15s ease; }
  #trend #xh { transition: transform .12s ease-out; }
  #trendTip {
    position: absolute; left: 0; top: 0; z-index: 2; pointer-events: none;
    background: #fff; border: 1px solid #e2e6ee; border-radius: 10px;
    padding: 10px 14px; min-width: 168px;
    opacity: 0; visibility: hidden;
    transition: transform .12s ease-out, opacity .12s ease, visibility .12s;
  }
  #trendTip.show { opacity: 1; visibility: visible; }
  #trendTip .tt-head { display: flex; justify-content: space-between; align-items: baseline; gap: 18px; }
  #trendTip .tt-date { font-family: var(--mono); font-size: 11px; color: var(--faint); }
  #trendTip .tt-total { font-family: var(--num); font-size: 13.5px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
  #trendTip .tt-div { height: 1px; background: #eef1f6; margin: 7px 0 5px; }
  #trendTip .tt-row { display: flex; align-items: center; gap: 8px; line-height: 22px; font-size: 12px; color: var(--dim); }
  #trendTip .tt-sq { width: 9px; height: 9px; border-radius: 2.5px; flex: 0 0 auto; }
  #trendTip .tt-val { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--text); font-variant-numeric: tabular-nums; padding-left: 18px; }

  svg { width: 100%; display: block; }
  svg g.barg {
    transform-box: fill-box; transform-origin: 50% 100%;
    animation: grow .45s cubic-bezier(.22,.8,.36,1) both;
  }
  @keyframes grow { from { transform: scaleY(0); } to { transform: scaleY(1); } }

  /* ---- 条形列表 ---- */
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 10px 0; font-size: 13px; }
  .bar-row .name { width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--dim); font-size: 13px; }
  .bar-row .track { flex: 1; display: block; background: #eef2fa; height: 10px; border-radius: 999px; overflow: hidden; }
  .bar-row .fill { display: block; height: 100%; border-radius: 999px; background: var(--blue); }
  .bar-row .num { width: 105px; text-align: right; font-family: var(--mono); font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums; }
  .empty { font-size: 12px; color: var(--faint); }
  .err { color: #e5484d; }

  /* ---- 页脚 ---- */
  .footer {
    margin-top: 26px; text-align: center;
    font-family: var(--mono); font-size: 11px; letter-spacing: .14em; color: var(--faint);
  }
  .footer .hex { font-size: 11px; margin-right: 6px; }

  @media (prefers-reduced-motion: reduce) {
    .reveal, .live-dot, svg g.barg { animation: none; }
  }
</style>
</head>
<body>

<canvas id="particles"></canvas>

<div class="topbar">
  <span class="hex">⬡</span>
  <span class="crumb">Kimi Board &nbsp;›&nbsp; <b>Token 用量</b></span>
  <span class="spacer"></span>
  <span class="live-dot"></span>
  <span id="updated"></span>
  <button id="refresh">刷新</button>
</div>

<div class="hero reveal">
  <div class="glyph-strip" aria-hidden="true">− + / ( ) * ▲ K # ⬡ &nbsp; − + / ( ) * ▲ K # ⬡</div>
  <div class="meta" id="heroMeta"></div>
</div>

<div class="cards reveal" id="cards" style="animation-delay:40ms"></div>

<div class="blackcard reveal" style="animation-delay:80ms">
  <div class="cost-grid">
    <div class="cost-left">
      <div class="glyph-field" aria-hidden="true"><div class="glyph-inner" id="glyphField"></div></div>
      <div class="label"><span class="hex" style="font-size:12px">⬡</span> 等效 API 费用 · 本月
        <span class="help"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.2"/><path d="M9.4 9a2.7 2.7 0 0 1 5.25.9c0 1.8-2.65 2.4-2.65 3.6"/><line x1="12" y1="16.8" x2="12.01" y2="16.8"/></svg><span class="tip">
          <span class="tp-f"><b>等效费用</b> = 缓存×缓存价 + 输入×输入价 + 输出×输出价</span>
          <span class="tp-t">
            <span class="tp-r tp-h"><b>元 / 1M</b><i>缓存</i><i>输入</i><i>输出</i></span>
            <span class="tp-r"><b>k3 / k3-256k</b><i>2</i><i>20</i><i>100</i></span>
            <span class="tp-r"><b>k2.7-highspeed</b><i>2.6</i><i>13</i><i>54</i></span>
            <span class="tp-r"><b>k2.7</b><i>1.3</i><i>6.5</i><i>27</i></span>
          </span>
          <span class="tp-note">刊例价估算 · 非实际账单 · 缓存创建按输入价计</span>
        </span></span>
      </div>
      <div class="big" id="costTotal">¥ --</div>
      <div class="sub" id="costBreakdown"></div>
    </div>
    <div class="payback" id="payback"></div>
  </div>
  <div class="model-cost" id="modelCost"></div>
  <div class="caption">COST · 04 PAYBACK · 刊例价估算非实际账单</div>
</div>

<section class="trendcard reveal" style="animation-delay:120ms">
  <div class="sec-head">用量趋势
    <span class="right seg" id="rangeSeg"><span class="seg-pill" aria-hidden="true"></span><button data-r="24h">24 小时</button><button data-r="7d" class="on">7 天</button><button data-r="30d">30 天</button><button data-r="mtd">本月</button></span>
  </div>
  <div class="trend-sum" id="trendSum"></div>
  <div class="trend-legend" id="trendLegend"></div>
  <svg id="trend" viewBox="0 0 1000 240" preserveAspectRatio="none" style="height:240px"></svg>
  <div id="trendTip"></div>
  <div class="caption">CHART · 05 TREND</div>
</section>

<div class="footer reveal" style="animation-delay:200ms"><span class="hex">⬡</span><span id="footMeta">数据来自本机 wire 文件 · 点刷新同步</span><a id="updateTip" class="update-tip" hidden target="_blank" rel="noopener"></a></div>

<script>
/* ---- 动效覆盖:访问 ?motion=on 后,即使系统开了"减少动画"也强制启用动效(存 localStorage);?motion=off 还原 ---- */
const _mq = new URLSearchParams(location.search).get("motion");
if (_mq === "on") localStorage.setItem("kb-motion", "on");
if (_mq === "off") localStorage.removeItem("kb-motion");
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches && localStorage.getItem("kb-motion") !== "on";

const fmt = n => n.toLocaleString("en-US");
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\\.0$/, "") + "M"
             : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\\.0$/, "") + "K" : String(n);
const yuan = n => "¥ " + n.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---- 粒子网络背景 ---- */
(function particles() {
  const cv = document.getElementById("particles");
  const ctx = cv.getContext("2d");
  const reduce = REDUCED;
  const LINK = 130, MLINK = 170;
  let W = 0, H = 0, pts = [], raf = null;
  const mouse = {x: -9e3, y: -9e3};

  function resize() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.min(110, Math.round(W * H / 18000));
    pts = Array.from({length: n}, () => ({
      x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - .5) * .35, vy: (Math.random() - .5) * .35,
      r: Math.random() * 1.6 + .8
    }));
  }

  function step() {
    ctx.clearRect(0, 0, W, H);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < -20) p.x = W + 20; else if (p.x > W + 20) p.x = -20;
      if (p.y < -20) p.y = H + 20; else if (p.y > H + 20) p.y = -20;
    }
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      for (let j = i + 1; j < pts.length; j++) {
        const b = pts[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy;
        if (d2 < LINK * LINK) {
          ctx.strokeStyle = `rgba(58,141,255,${((1 - Math.sqrt(d2) / LINK) * .18).toFixed(3)})`;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      const mdx = a.x - mouse.x, mdy = a.y - mouse.y, md2 = mdx * mdx + mdy * mdy;
      if (md2 < MLINK * MLINK) {
        ctx.strokeStyle = `rgba(46,111,232,${((1 - Math.sqrt(md2) / MLINK) * .3).toFixed(3)})`;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke();
      }
      ctx.fillStyle = "rgba(58,141,255,.4)";
      ctx.beginPath(); ctx.arc(a.x, a.y, a.r, 0, 6.2832); ctx.fill();
    }
  }

  function loop() { step(); raf = requestAnimationFrame(loop); }
  addEventListener("resize", resize);
  addEventListener("mousemove", e => { mouse.x = e.clientX; mouse.y = e.clientY; });
  document.addEventListener("mouseleave", () => { mouse.x = -9e3; mouse.y = -9e3; });
  resize();
  if (reduce) { step(); return; }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = null; }
    else if (!raf) loop();
  });
  loop();
})();

/* ---- 黑卡字符场（动态变换） ---- */
(function glyphField() {
  const el = document.getElementById("glyphField");
  if (!el) return;
  const CHARS = "-+/()*▲K#⬡=x";
  const ROWS = 4, COLS = 52;
  const cells = [], ts = [];
  const frag = document.createDocumentFragment();
  const rnd = () => CHARS[Math.floor(Math.random() * CHARS.length)];
  const alpha = t => (0.16 + Math.random() * 0.2 + 0.1 * t).toFixed(2);

  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) {
      const t = c / (COLS - 1);
      const s = document.createElement("span");
      if (Math.random() < 0.8 + 0.2 * t) {
        s.textContent = rnd();
        s.style.opacity = alpha(t);
        if (Math.random() < 0.22) s.style.color = "#5ea2ff";
        s.dataset.on = "1";
      } else {
        s.textContent = " ";
      }
      cells.push(s); ts.push(t); frag.appendChild(s);
    }
    if (r < ROWS - 1) frag.appendChild(document.createTextNode("\\n"));
  }
  el.appendChild(frag);

  if (REDUCED) return;
  setInterval(() => {
    if (document.hidden) return;
    for (let i = 0; i < 6; i++) {
      const k = Math.floor(Math.random() * cells.length);
      const s = cells[k], t = ts[k];
      if (s.dataset.on) {
        if (Math.random() < 0.8) { s.textContent = rnd(); }
        else { s.style.color = s.style.color ? "" : "#5ea2ff"; }
      } else if (Math.random() < 0.8 + 0.2 * t) {
        s.textContent = rnd();
        s.style.opacity = alpha(t);
        s.dataset.on = "1";
      }
    }
  }, 300);
})();

/* ---- 数字滚动 ---- */
function countUp(el, target, format) {
  if (REDUCED || !(target > 0)) { el.textContent = format(target); return; }
  const t0 = performance.now(), dur = 650;
  (function tick(t) {
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = format(target * e);
    if (k < 1) requestAnimationFrame(tick); else el.textContent = format(target);
  })(t0);
}

function cardHtml(label, cap, c, tint) {
  const hitRate = c.input ? (c.cacheRead / c.input * 100).toFixed(1) : "0.0";
  return `<div class="card${tint ? " tint" : ""}">
    <div class="label"><span class="hex">⬡</span>${label}</div>
    <div class="value" data-v="${c.total}">0</div>
    <div class="sub">
      <div class="row"><span>输入</span><span class="num">${fmt(c.input)}</span></div>
      <div class="row"><span>其中缓存读取</span><span class="num">${fmt(c.cacheRead)}</span></div>
      <div class="row"><span>输出</span><span class="num">${fmt(c.output)}</span></div>
    </div>
    <span class="pill">缓存命中率 ${hitRate}%</span>
    <div class="caption">${cap}</div>
  </div>`;
}

/* ---- 用量趋势:单图 + 范围切换(选择存 localStorage);按模型堆叠(DeepSeek 式) ---- */
const TREND = { range: localStorage.getItem("kb-range") || "7d", data: null, order: [], color: {} };
const TREND_PALETTE = ["#2e6fe8", "#5ea2ff", "#9ec4ff", "#c9ddff", "#dfe9fa"]; // 末位为"其他"
// 固定色：能力越强的模型颜色越深（k3 > k3-256k > k2.7-highspeed > k2.7）
const MODEL_COLOR = {
  "kimi-code/k3": "#2e6fe8",
  "kimi-code/k3-256k": "#5ea2ff",
  "kimi-code/kimi-for-coding-highspeed": "#9ec4ff",
  "kimi-code/kimi-for-coding": "#c9ddff",
};
// 固定排序：同样按能力从高到低
const MODEL_ORDER = ["kimi-code/k3", "kimi-code/k3-256k",
  "kimi-code/kimi-for-coding-highspeed", "kimi-code/kimi-for-coding"];
// 显示名：对齐 CLI 里的叫法（k2.7 = K2.7 Coding）
const MODEL_LABEL = {
  "kimi-code/k3": "k3",
  "kimi-code/k3-256k": "k3-256k",
  "kimi-code/kimi-for-coding-highspeed": "k2.7-highspeed",
  "kimi-code/kimi-for-coding": "k2.7",
};
const shortName = m => m === "其他" ? m : (MODEL_LABEL[m] || m.replace(/^kimi-code\//, ""));

function trendLabel(p, kind, long) {
  const dt = new Date(p.t);
  const iso = dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0");
  if (kind === "hour") {
    const hh = String(dt.getHours()).padStart(2, "0") + ":00";
    return long ? iso + " " + hh : hh;
  }
  return long ? iso : (dt.getMonth() + 1) + "/" + dt.getDate();
}

function trendPoints() {
  const d = TREND.data;
  if (TREND.range === "24h") return { pts: d.hourly, kind: "hour" };
  if (TREND.range === "7d") return { pts: d.daily.slice(-7), kind: "day" };
  if (TREND.range === "30d") return { pts: d.daily.slice(-30), kind: "day" };
  const now = new Date();
  const pts = d.daily.filter(p => {
    const dt = new Date(p.t);
    return dt.getFullYear() === now.getFullYear() && dt.getMonth() === now.getMonth();
  });
  // 补齐本月未来日期(空桶),让"本月"显示整月
  const dim = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  for (let day = now.getDate() + 1; day <= dim; day++) {
    pts.push({ t: new Date(now.getFullYear(), now.getMonth(), day).getTime(),
               input: 0, cacheRead: 0, output: 0, total: 0, models: {} });
  }
  return { pts, kind: "day" };
}

/* 取某桶内归属 key(前 4 模型或"其他")的用量 */
function segValue(p, key) {
  let sv = 0;
  for (const [name, val] of Object.entries(p.models || {})) {
    if ((TREND.color[name] ? name : "其他") === key) sv += val;
  }
  return sv;
}

function renderTrend() {
  if (!TREND.data) return;
  const { pts, kind } = trendPoints();
  let tot = 0, peak = pts[0];
  const modelSums = {};
  for (const p of pts) {
    tot += p.total;
    if (p.total > peak.total) peak = p;
    for (const m of TREND.order) {
      const sv = segValue(p, m);
      if (sv > 0) modelSums[m] = (modelSums[m] || 0) + sv;
    }
  }
  const avg = pts.length ? Math.round(tot / pts.length) : 0;
  document.getElementById("trendSum").innerHTML = `
    <div class="ts"><span class="lb">合计</span><span class="vl"><b id="tsTotal">0</b><span class="unit">tokens</span></span></div>
    <div class="ts"><span class="lb">峰值</span><span class="vl"><b>${fmtK(peak.total)}</b><span class="unit">${esc(kind === "hour" ? trendLabel(peak, kind, true).slice(5) : trendLabel(peak, kind, true))}</span></span></div>
    <div class="ts"><span class="lb">${kind === "hour" ? "时均" : "日均"}</span><span class="vl"><b>${fmtK(avg)}</b><span class="unit">tokens</span></span></div>`;
  countUp(document.getElementById("tsTotal"), tot, v => fmtK(Math.round(v)));
  document.getElementById("trendLegend").innerHTML = TREND.order
    .filter(m => modelSums[m] > 0)
    .map(m => `<span class="lg"><span class="sq" style="background:${TREND.color[m]}"></span>${esc(shortName(m))} <span class="amt">${fmtK(modelSums[m])}</span></span>`).join("");
  barChart(document.getElementById("trend"), pts, kind);
}

function placeSegPill() {
  const seg = document.getElementById("rangeSeg");
  const pill = seg.querySelector(".seg-pill");
  const on = seg.querySelector("button.on");
  if (!on) return;
  pill.style.left = on.offsetLeft + "px";
  pill.style.width = on.offsetWidth + "px";
}

document.querySelectorAll("#rangeSeg button").forEach(b => b.onclick = () => {
  TREND.range = b.dataset.r;
  localStorage.setItem("kb-range", TREND.range);
  document.querySelectorAll("#rangeSeg button").forEach(x => x.classList.toggle("on", x === b));
  placeSegPill();
  renderTrend();
});
requestAnimationFrame(placeSegPill);

let trendRszT = 0;
addEventListener("resize", () => {
  clearTimeout(trendRszT);
  trendRszT = setTimeout(() => { placeSegPill(); renderTrend(); }, 150);
});

function barChart(el, points, kind) {
  const H = 240, PADL = 44, PADR = 14, PADT = 14, PADB = 26;
  const W = Math.max(320, Math.round(el.getBoundingClientRect().width)
    || (el.parentElement ? el.parentElement.clientWidth : 0) || 1000);
  el.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const vals = points.map(p => p.total);
  const rawMax = Math.max(...vals, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rawMax)));
  const max = Math.ceil(rawMax / mag * 2) / 2 * mag;
  const n = points.length;
  const slot = (W - PADL - PADR) / n;
  const bw = Math.min(64, Math.max(2, slot * 0.62));
  const x = i => PADL + i * slot + (slot - bw) / 2;
  const cx = i => PADL + i * slot + slot / 2;
  const y = v => PADT + (1 - v / max) * (H - PADT - PADB);
  const base = H - PADB;

  let grid = "", ylabels = "";
  for (let g = 0; g <= 4; g++) {
    const gv = max * (1 - g / 4), gy = PADT + (g / 4) * (H - PADT - PADB);
    grid += `<line x1="${PADL}" y1="${gy}" x2="${W - PADR}" y2="${gy}" stroke="${g === 4 ? "#dde6f3" : "#ecf2fb"}" stroke-width="1"/>`;
    ylabels += `<text x="${PADL - 8}" y="${gy + 3.5}" font-size="10" fill="#a8b4cc" text-anchor="end" font-family="ui-monospace,Consolas,monospace">${fmtK(gv)}</text>`;
  }

  const bars = vals.map((v, i) => {
    if (v <= 0) return "";
    const segs = TREND.order.map(m => ({ m, v: segValue(points[i], m) })).filter(s => s.v > 0);
    const r = Math.min(3, bw / 2);
    let acc = 0, out = "";
    segs.forEach((s, si) => {
      const y1 = y(acc + s.v), y0 = y(acc);
      const rx = si === segs.length - 1 ? (segs.length === 1 ? r : Math.min(2, r)) : 0;
      out += `<rect class="bar" data-i="${i}" x="${x(i).toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${(y0 - y1).toFixed(1)}" rx="${rx}" fill="${TREND.color[s.m]}"/>`;
      acc += s.v;
    });
    return `<g class="barg">${out}</g>`;
  }).join("");

  const step = Math.ceil(n / 8);
  const xlabels = points.map((p, i) => i % step ? "" :
    `<text x="${cx(i).toFixed(1)}" y="${H - 7}" font-size="10" fill="#a8b4cc" text-anchor="middle" font-family="ui-monospace,Consolas,monospace">${esc(trendLabel(p, kind))}</text>`).join("");

  el.innerHTML = `${grid}${ylabels}${bars}${xlabels}
    <line id="xh" x1="0" y1="${PADT}" x2="0" y2="${base}" stroke="#2e6fe8" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>`;

  const barEls = el.querySelectorAll("rect.bar");
  const xh = el.querySelector("#xh");
  const tip = document.getElementById("trendTip");
  const sec = el.closest("section");
  el.onmousemove = e => {
    const r = el.getBoundingClientRect();
    const vx = (e.clientX - r.left) / r.width * W;
    const i = Math.floor((vx - PADL) / slot);
    if (i < 0 || i >= n) { el.onmouseleave(); return; }
    const p = points[i], v = vals[i];
    xh.style.transform = `translateX(${cx(i)}px)`;
    xh.setAttribute("visibility", "visible");
    barEls.forEach(b => b.style.opacity = +b.dataset.i === i ? 1 : .3);
    const rows = TREND.order.map(m => ({ m, v: segValue(p, m) })).filter(s => s.v > 0);
    tip.innerHTML = `<div class="tt-head"><span class="tt-date">${esc(trendLabel(p, kind, true))}</span><span class="tt-total">${fmt(v)}</span></div>` +
      (rows.length ? `<div class="tt-div"></div>` + rows.map(s =>
        `<div class="tt-row"><span class="tt-sq" style="background:${TREND.color[s.m]}"></span>${esc(shortName(s.m))}<span class="tt-val">${fmtK(s.v)}</span></div>`).join("") : "");
    const sr = sec.getBoundingClientRect();
    const relX = e.clientX - sr.left, relY = e.clientY - sr.top;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let tx = relX + 14;
    if (tx + tw > sr.width - 8) tx = relX - tw - 14;
    let ty = relY - th - 12;
    if (ty < 4) ty = relY + 18;
    tip.style.transform = `translate(${tx.toFixed(1)}px, ${ty.toFixed(1)}px)`;
    tip.classList.add("show");
  };
  el.onmouseleave = () => {
    xh.setAttribute("visibility", "hidden");
    tip.classList.remove("show");
    barEls.forEach(b => b.style.opacity = 1);
  };
}

function modelCostList(el, rows) {
  if (!rows.length) { el.innerHTML = '<span class="empty">暂无数据</span>'; return; }
  const max = Math.max(...rows.map(r => Math.max(r.cost, r.prevCost)), 0.01);
  el.innerHTML = '<div class="mc-legend"><span class="sq1">■</span> 本月 &nbsp; <span class="sq2">■</span> 上月</div>' + rows.map(r => `<div class="bar-row" title="${esc(r.name)}">
    <span class="name">${esc(shortName(r.name))}</span>
    <span class="track">
      <span class="fill" style="width:${(r.cost / max * 100).toFixed(1)}%"></span>
      <span class="fill prev" style="width:${(r.prevCost / max * 100).toFixed(1)}%"></span>
    </span>
    <span class="num">${yuan(r.cost)}<span class="prev-num">上月 ${yuan(r.prevCost)}</span></span>
  </div>`).join("");
}

function paybackHtml(c) {
  const planLabel = c.planName
    ? `${c.planName} · ¥${c.planPrice} 套餐`
    : `¥${c.planPrice} 套餐（默认，可 --plan-price 指定）`;
  if (c.planPrice <= 0 || c.paybackPct === null) {
    return `<div class="pb-label">${c.planName || "免费"} 套餐 · 无月费</div>
      <div class="pb-value">¥0</div>
      <div class="pb-note">本月等效用量价值 ${yuan(c.monthTotal)}</div>
      <div class="pb-note">上月（${c.prevMonthLabel}）等效 ${yuan(c.prevMonthTotal)}</div>`;
  }
  const pct = c.paybackPct;
  const w = Math.min(pct, 100).toFixed(1);
  const verdict = pct >= 100
    ? `已回本 ${(pct / 100).toFixed(1)} 倍`
    : `还差 ${yuan(c.planPrice - c.monthTotal)} 回本`;
  const pace = `当前节奏 · 月底预估 ${yuan(c.pace)}`;
  return `<div class="pb-label">${planLabel} · 本月回本率</div>
    <div class="pb-value">${pct}%</div>
    <div class="pb-bar"><div class="pb-fill" style="width:${w}%"></div></div>
    <div class="pb-note">${verdict} · ${pace}</div>
    <div class="pb-note">上月（${c.prevMonthLabel}）等效 ${yuan(c.prevMonthTotal)}</div>`;
}

async function load() {
  const updated = document.getElementById("updated");
  try {
    const d = await (await fetch("/api/stats", {cache: "no-store"})).json();
    document.getElementById("heroMeta").innerHTML =
      `<span class="hex">⬡</span>${esc((d.cost.planName || "FREE PLAN").toUpperCase())} · ${fmt(d.turns)} TURNS TRACKED`;
    document.getElementById("footMeta").textContent =
      `数据来自本机 wire 文件 · 更新于 ${new Date(d.generatedAt).toLocaleTimeString("zh-CN", {hour12: false})} · 点刷新同步`;

    document.getElementById("cards").innerHTML =
      cardHtml("本月", "USAGE · 01 MONTH", d.cards.month, true) +
      cardHtml("今日", "USAGE · 02 TODAY", d.cards.today, false) +
      cardHtml("近 1 小时", "USAGE · 03 HOUR", d.cards.hour, false);
    document.querySelectorAll("#cards .value").forEach(el =>
      countUp(el, +el.dataset.v, v => fmt(Math.round(v))));

    const c = d.cost;
    countUp(document.getElementById("costTotal"), c.monthTotal, yuan);
    document.getElementById("costBreakdown").innerHTML = `
      <div class="row"><span>缓存命中</span><span class="num">${yuan(c.components.cache)}</span></div>
      <div class="row"><span>输入（未命中）</span><span class="num">${yuan(c.components.miss)}</span></div>
      <div class="row"><span>输出</span><span class="num">${yuan(c.components.out)}</span></div>`;
    document.getElementById("payback").innerHTML = paybackHtml(c);
    modelCostList(document.getElementById("modelCost"), c.byModel);

    TREND.data = d;
    const _sums = {};
    for (const p of [...d.hourly, ...d.daily])
      for (const [m, v] of Object.entries(p.models || {})) _sums[m] = (_sums[m] || 0) + v;
    const _top = Object.entries(_sums).sort((a, b) => b[1] - a[1]).map(e => e[0]);
    const _top4 = _top.slice(0, 4);
    TREND.order = [
      ...MODEL_ORDER.filter(m => _top4.includes(m)),   // 已知模型按能力排序
      ..._top4.filter(m => !MODEL_ORDER.includes(m)),  // 未知模型按用量排其后
    ];
    if (_top.length > 4) TREND.order.push("其他");
    TREND.color = {};
    const _free = TREND_PALETTE.slice(0, -1).filter(c => !Object.values(MODEL_COLOR).includes(c));
    let _fi = 0;
    TREND.order.forEach(m => {
      TREND.color[m] = m === "其他" ? TREND_PALETTE[TREND_PALETTE.length - 1]
        : (MODEL_COLOR[m] || _free[_free.length ? Math.min(_fi++, _free.length - 1) : 0]
           || TREND_PALETTE[TREND_PALETTE.length - 1]);
    });
    document.querySelectorAll("#rangeSeg button").forEach(x =>
      x.classList.toggle("on", x.dataset.r === TREND.range));
    placeSegPill();
    renderTrend();
    updated.textContent = `更新于 ${new Date(d.generatedAt).toLocaleString("zh-CN", {hour12: false})} · ${fmt(d.turns)} turns`;
    updated.classList.remove("err");
  } catch (e) {
    updated.textContent = "加载失败：" + e;
    updated.classList.add("err");
  }
}
document.getElementById("refresh").onclick = load;
load();

// 自动检查新版本：有更新时在页头显示 pill，点击直达 release 页
(async () => {
  const get = () => fetch("/api/version", {cache: "no-store"}).then(r => r.json());
  try {
    let v = await get();
    if (!v.latest) {  // 后端正在后台查询，稍等再取一次
      await new Promise(r => setTimeout(r, 9000));
      v = await get();
    }
    if (v.newer && v.url) {
      const tip = document.getElementById("updateTip");
      tip.textContent = `· ⬆ v${v.latest} 可更新`;
      tip.href = v.url;
      tip.hidden = false;
    }
  } catch (e) { /* 离线或接口失败时静默 */ }
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- 服务


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path.startswith("/api/stats"):
            body = json.dumps(collect_stats(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/api/version"):
            body = json.dumps(check_update(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许浏览器扩展从 kimi webui 页面探测服务状态
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global PLAN_PRICE
    ap = argparse.ArgumentParser(description="kimi code token 消耗网页看板")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--plan-price", type=float, default=None,
                    help="会员月费（默认自动识别；识别失败时按 199 计）")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()
    PLAN_PRICE = args.plan_price

    # pythonw / 无控制台环境下 sys.stdout、sys.stderr 为 None,
    # print 与请求日志会抛 AttributeError,重定向到空设备
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"kimi code token 看板已启动：{url}  (Ctrl+C 停止)")
    if not args.no_open:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
