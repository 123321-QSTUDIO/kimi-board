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
    "kimi-code/k3": (2.0, 20.0, 100.0),
    "kimi-code/k3-256k": (2.0, 20.0, 100.0),          # k3 的 256k 变体，按 k3 价
    "kimi-code/kimi-for-coding": (1.3, 6.5, 27.0),    # 对应 kimi-k2.7-code
}
DEFAULT_PRICE = (2.0, 20.0, 100.0)

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
    结果缓存 10 分钟。
    """
    import time
    import urllib.request

    now = time.time()
    if _plan_cache["at"] and now - _plan_cache["at"] < 600:
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


def load_workdir_names(home: Path):
    """sessionId -> workDir 路径（来自 session_index.jsonl）。"""
    names = {}
    idx = home / "session_index.jsonl"
    try:
        with idx.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid, wd = rec.get("sessionId"), rec.get("workDir")
                if sid and wd:
                    names[sid] = wd.replace("/", "\\").rstrip("\\")
    except OSError:
        pass
    return names


def empty_usage():
    return {k: 0 for k in USAGE_KEYS}


def cost_of(model: str, u: dict) -> float:
    """按刊例价估算费用（元）。inputCacheCreation 按未命中输入计。"""
    hit, miss, out = PRICING.get(model, DEFAULT_PRICE)
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
    day_bucket0 = int(day0.timestamp() * 1000) - 29 * 86400 * 1000

    cards = {"month": empty_usage(), "today": empty_usage(), "hour": empty_usage()}
    hourly = [empty_usage() for _ in range(24)]
    daily = [empty_usage() for _ in range(30)]
    models = defaultdict(lambda: empty_usage())
    models_month = defaultdict(lambda: empty_usage())
    models_prev = defaultdict(lambda: empty_usage())
    workdirs = defaultdict(lambda: empty_usage())
    wd_names = load_workdir_names(home)
    turns = 0

    if sessions_dir.is_dir():
        for wire in sessions_dir.glob("*/*/agents/*/wire.jsonl"):
            for t, model, sid, usage in scan_wire_file(wire):
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
                    add(hourly[(t - hour_bucket0) // (3600 * 1000)])
                if t >= day_bucket0:
                    add(daily[(t - day_bucket0) // (86400 * 1000)])
                add(models[model])
                add(workdirs[wd_names.get(sid, sid)])

    def finalize(u):
        inp = u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"]
        return {
            "input": inp,
            "cacheRead": u["inputCacheRead"],
            "output": u["output"],
            "total": inp + u["output"],
        }

    def top(counter, n=10):
        rows = sorted(
            ((name, finalize(u)) for name, u in counter.items()),
            key=lambda r: -r[1]["total"],
        )
        return [{"name": name, **u} for name, u in rows[:n]]

    # ---- 费用估算 ----
    cost_by_model = []
    month_cost = cache_cost = miss_cost = out_cost = 0.0
    for model, u in models_month.items():
        hit, miss, out = PRICING.get(model, DEFAULT_PRICE)
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
            {"t": hour_bucket0 + i * 3600 * 1000, **finalize(u)} for i, u in enumerate(hourly)
        ],
        "daily": [
            {"t": day_bucket0 + i * 86400 * 1000, **finalize(u)} for i, u in enumerate(daily)
        ],
        "models": top(models),
        "workdirs": top(workdirs),
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
    --bg: #f7f9fd;
    --card: #ffffff;
    --tint: #dcebff;
    --line: #e5eaf2;
    --text: #101828;
    --dim: #667085;
    --faint: #a8b4cc;
    --blue: #3a8dff;
    --blue-deep: #2e6fe8;
    --ink: #0c0e12;
    --mono: ui-monospace, "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
    --sans: "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; }
  html { background: var(--bg); }
  body {
    background: transparent; color: var(--text);
    font-family: var(--sans); padding: 26px 28px 48px;
    max-width: 1160px; margin: 0 auto;
  }

  /* ---- 粒子背景 ---- */
  #particles { position: fixed; inset: 0; z-index: -1; pointer-events: none; }

  /* ---- 进场动效 ---- */
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
  .reveal { animation: rise .5s cubic-bezier(.22,.8,.36,1) both; }

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

  /* ---- 主标题 ---- */
  .hero { position: relative; margin-bottom: 30px; padding: 6px 0 2px; }
  .hero h1 { font-size: 56px; font-weight: 800; letter-spacing: -.02em; line-height: 1.15; }
  .hero h1 em { font-style: normal; color: var(--blue); }
  .hero .meta {
    margin-top: 14px; min-height: 15px;
    font-family: var(--mono); font-size: 11px; letter-spacing: .18em;
    color: var(--dim); text-transform: uppercase;
  }
  .glyph-strip {
    position: absolute; right: 0; top: 50%; transform: translateY(-50%);
    font-family: var(--mono); font-size: 14px; letter-spacing: .3em; white-space: nowrap;
    color: var(--blue); opacity: .5; user-select: none;
    -webkit-mask-image: linear-gradient(90deg, transparent, #000 65%);
    mask-image: linear-gradient(90deg, transparent, #000 65%);
  }
  @media (max-width: 860px) {
    .hero h1 { font-size: 38px; }
    .glyph-strip { display: none; }
  }

  /* ---- 卡片通用 ---- */
  .card, section, .blackcard {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 22px 24px;
  }
  .card, section { transition: border-color .18s ease, transform .18s ease; }
  .card:hover, section:hover { border-color: #c9dfff; transform: translateY(-1px); }
  .caption {
    margin-top: 16px; font-family: var(--mono); font-size: 10px;
    letter-spacing: .3em; color: var(--faint); text-transform: uppercase;
  }
  .card .label {
    font-size: 13px; color: var(--dim); font-weight: 550;
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  }
  .card .label .hex { font-size: 12px; }
  .card .value { font-size: 44px; font-weight: 800; letter-spacing: -.02em; font-variant-numeric: tabular-nums; margin-bottom: 12px; }
  .card .sub { font-size: 12.5px; color: var(--dim); line-height: 2.0; }
  .card .sub .row { display: flex; justify-content: space-between; gap: 12px; }
  .card .sub .num { font-family: var(--mono); color: var(--text); font-variant-numeric: tabular-nums; }
  .pill {
    display: inline-block; margin-top: 10px; font-size: 11.5px; font-weight: 600;
    color: var(--blue-deep); background: rgba(58,141,255,.12);
    border-radius: 999px; padding: 3px 12px; font-family: var(--mono);
  }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card.tint { background: var(--tint); border-color: #c9dfff; }
  .card.tint .caption { color: #7fa3d8; }

  /* ---- 黑色费用卡 ---- */
  .blackcard { background: var(--ink); border-color: var(--ink); color: #fff; margin-bottom: 16px; }
  .blackcard .label { font-size: 13px; color: #9aa4b8; font-weight: 550; display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .blackcard .big { font-size: 46px; font-weight: 800; letter-spacing: -.02em; font-variant-numeric: tabular-nums; margin-bottom: 14px; }
  .blackcard .sub { font-size: 12.5px; color: #9aa4b8; line-height: 2.0; }
  .blackcard .sub .row { display: flex; justify-content: space-between; gap: 12px; }
  .blackcard .sub .num { font-family: var(--mono); color: #fff; font-variant-numeric: tabular-nums; }
  .blackcard .caption { color: #4b5261; }
  .cost-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 22px; }
  @media (max-width: 860px) { .cost-grid { grid-template-columns: 1fr; } }
  .payback {
    background: var(--blue); border-radius: 14px; padding: 18px 20px; color: #fff;
    display: flex; flex-direction: column; justify-content: center; gap: 8px;
  }
  .payback .pb-label { font-size: 13px; opacity: .85; }
  .payback .pb-value { font-size: 34px; font-weight: 800; font-variant-numeric: tabular-nums; }
  .payback .pb-bar { height: 8px; background: rgba(255,255,255,.3); border-radius: 999px; overflow: hidden; }
  .payback .pb-fill { height: 100%; background: #fff; border-radius: 999px; }
  .payback .pb-note { font-size: 12px; opacity: .85; }
  .model-cost { margin-top: 14px; border-top: 1px solid #232733; padding-top: 12px; }
  .model-cost .mc-legend { font-size: 11px; color: #6b7385; margin-bottom: 8px; font-family: var(--mono); }
  .model-cost .mc-legend .sq1 { color: var(--blue); }
  .model-cost .mc-legend .sq2 { color: #3a4152; }
  .model-cost .bar-row .name { color: #9aa4b8; }
  .model-cost .bar-row .track {
    background: transparent; display: flex; flex-direction: column;
    gap: 3px; height: auto; overflow: visible; border-radius: 0;
  }
  .model-cost .bar-row .fill { background: var(--blue); height: 8px; }
  .model-cost .bar-row .fill.prev { background: #3a4152; height: 5px; }
  .model-cost .bar-row .num {
    color: #fff; width: 130px; display: flex; flex-direction: column;
    align-items: flex-end; line-height: 1.6;
  }
  .model-cost .prev-num { font-size: 10px; color: #6b7385; }

  /* ---- 区块 ---- */
  section { margin-bottom: 16px; }
  .sec-head { font-size: 14px; font-weight: 650; margin-bottom: 14px; display: flex; align-items: baseline; gap: 10px; }
  .sec-head .right { margin-left: auto; font-size: 11px; color: var(--faint); font-family: var(--mono); font-weight: 400; letter-spacing: .1em; }
  svg { width: 100%; display: block; }
  svg rect.bar {
    transform-box: fill-box; transform-origin: 50% 100%;
    animation: grow .45s cubic-bezier(.22,.8,.36,1) both;
  }
  @keyframes grow { from { transform: scaleY(0); } to { transform: scaleY(1); } }

  /* ---- 条形列表 ---- */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 10px 0; font-size: 12.5px; }
  .bar-row .name { width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--dim); font-size: 12px; }
  .bar-row .track { flex: 1; display: block; background: #eef2fa; height: 10px; border-radius: 999px; overflow: hidden; }
  .bar-row .fill { display: block; height: 100%; border-radius: 999px; background: var(--blue); }
  .bar-row .num { width: 105px; text-align: right; font-family: var(--mono); font-size: 11.5px; color: var(--text); font-variant-numeric: tabular-nums; }
  .empty { font-size: 12px; color: var(--faint); }
  .err { color: #e5484d; }

  @media (prefers-reduced-motion: reduce) {
    .reveal, .live-dot, svg rect.bar { animation: none; }
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
  <h1>本机 <em>token</em> 用量</h1>
  <div class="meta" id="heroMeta"></div>
</div>

<div class="cards reveal" id="cards" style="animation-delay:60ms"></div>

<div class="blackcard reveal" style="animation-delay:120ms">
  <div class="cost-grid">
    <div>
      <div class="label"><span class="hex" style="font-size:12px">⬡</span> 等效 API 费用 · 本月</div>
      <div class="big" id="costTotal">¥ --</div>
      <div class="sub" id="costBreakdown"></div>
    </div>
    <div class="payback" id="payback"></div>
  </div>
  <div class="model-cost" id="modelCost"></div>
  <div class="caption">COST · 04 PAYBACK · 刊例价估算非实际账单</div>
</div>

<section class="reveal" style="animation-delay:180ms">
  <div class="sec-head">最近 24 小时 <span class="right">TOKENS / HOUR</span></div>
  <svg id="hourly" viewBox="0 0 1000 240" preserveAspectRatio="none" style="height:240px"></svg>
  <div class="caption">CHART · 05 HOURLY</div>
</section>

<section class="reveal" style="animation-delay:240ms">
  <div class="sec-head">最近 30 天 <span class="right">TOKENS / DAY</span></div>
  <svg id="daily" viewBox="0 0 1000 240" preserveAspectRatio="none" style="height:240px"></svg>
  <div class="caption">CHART · 06 DAILY</div>
</section>

<div class="grid2 reveal" style="animation-delay:300ms">
  <section>
    <div class="sec-head">按模型</div>
    <div id="models"></div>
    <div class="caption">RANK · 07 MODEL</div>
  </section>
  <section>
    <div class="sec-head">按工作目录</div>
    <div id="workdirs"></div>
    <div class="caption">RANK · 08 WORKDIR</div>
  </section>
</div>

<script>
const fmt = n => n.toLocaleString("en-US");
const fmtK = n => n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\\.0$/, "") + "M"
             : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\\.0$/, "") + "K" : String(n);
const yuan = n => "¥ " + n.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---- 粒子网络背景 ---- */
(function particles() {
  const cv = document.getElementById("particles");
  const ctx = cv.getContext("2d");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
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

/* ---- 数字滚动 ---- */
function countUp(el, target, format) {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !(target > 0)) { el.textContent = format(target); return; }
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

function barChart(el, points, labelFn) {
  const W = 1000, H = 240, PADL = 44, PADR = 14, PADT = 14, PADB = 26;
  const vals = points.map(p => p.total);
  const rawMax = Math.max(...vals, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rawMax)));
  const max = Math.ceil(rawMax / mag * 2) / 2 * mag;
  const n = points.length;
  const slot = (W - PADL - PADR) / n;
  const bw = Math.max(2, slot * 0.62);
  const x = i => PADL + i * slot + (slot - bw) / 2;
  const y = v => PADT + (1 - v / max) * (H - PADT - PADB);
  const base = H - PADB;

  let grid = "", ylabels = "";
  for (let g = 0; g <= 4; g++) {
    const gv = max * (1 - g / 4), gy = PADT + (g / 4) * (H - PADT - PADB);
    grid += `<line x1="${PADL}" y1="${gy}" x2="${W - PADR}" y2="${gy}" stroke="${g === 4 ? "#dde6f3" : "#ecf2fb"}" stroke-width="1"/>`;
    ylabels += `<text x="${PADL - 8}" y="${gy + 3.5}" font-size="10" fill="#a8b4cc" text-anchor="end" font-family="ui-monospace,Consolas,monospace">${fmtK(gv)}</text>`;
  }

  let lastIdx = -1;
  for (let i = n - 1; i >= 0; i--) if (vals[i] > 0) { lastIdx = i; break; }
  const bars = vals.map((v, i) => {
    if (v <= 0) return "";
    const bh = base - y(v), r = Math.min(3, bw / 2);
    const fill = i === lastIdx ? "#2e6fe8" : "#3a8dff";
    return `<rect class="bar" x="${x(i).toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="${r}" fill="${fill}">
      <title>${esc(labelFn(points[i]))}: ${fmt(v)}</title></rect>`;
  }).join("");

  const step = Math.ceil(n / 8);
  const xlabels = points.map((p, i) => i % step ? "" :
    `<text x="${(PADL + i * slot + slot / 2).toFixed(1)}" y="${H - 7}" font-size="10" fill="#a8b4cc" text-anchor="middle" font-family="ui-monospace,Consolas,monospace">${esc(labelFn(p))}</text>`).join("");

  el.innerHTML = `${grid}${ylabels}${bars}${xlabels}`;
}

function barList(el, rows, unit) {
  if (!rows.length) { el.innerHTML = '<span class="empty">暂无数据</span>'; return; }
  const max = Math.max(...rows.map(r => r.value), 0.01);
  el.innerHTML = rows.map(r => `<div class="bar-row" title="${esc(r.name)}">
    <span class="name">${esc(r.name)}</span>
    <span class="track"><span class="fill" style="width:${(r.value / max * 100).toFixed(1)}%"></span></span>
    <span class="num">${unit ? unit(r.value) : fmt(r.value)}</span>
  </div>`).join("");
}

function modelCostList(el, rows) {
  if (!rows.length) { el.innerHTML = '<span class="empty">暂无数据</span>'; return; }
  const max = Math.max(...rows.map(r => Math.max(r.cost, r.prevCost)), 0.01);
  el.innerHTML = '<div class="mc-legend"><span class="sq1">■</span> 本月 &nbsp; <span class="sq2">■</span> 上月</div>' + rows.map(r => `<div class="bar-row" title="${esc(r.name)}">
    <span class="name">${esc(r.name)}</span>
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
    document.getElementById("heroMeta").textContent =
      `${(d.cost.planName || "FREE PLAN").toUpperCase()} · ${fmt(d.turns)} TURNS TRACKED`;

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

    barChart(document.getElementById("hourly"), d.hourly,
      p => String(new Date(p.t).getHours()).padStart(2, "0") + ":00");
    barChart(document.getElementById("daily"), d.daily, p => {
      const dt = new Date(p.t);
      return String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0");
    });
    barList(document.getElementById("models"), d.models.map(m => ({name: m.name, value: m.total})));
    barList(document.getElementById("workdirs"), d.workdirs.map(w => ({name: w.name, value: w.total})));
    updated.textContent = `更新于 ${new Date(d.generatedAt).toLocaleString("zh-CN", {hour12: false})} · ${fmt(d.turns)} turns`;
    updated.classList.remove("err");
  } catch (e) {
    updated.textContent = "加载失败：" + e;
    updated.classList.add("err");
  }
}
document.getElementById("refresh").onclick = load;
load();
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
