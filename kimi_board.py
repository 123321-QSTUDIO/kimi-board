#!/usr/bin/env python3
"""kimi_board.py — kimi code token 消耗本地网页看板（单文件、零依赖、可迁移）。

独立小服务（仅标准库），与 `kimi web` 并存，打开页面时统计一次，
页面上的"刷新"按钮重新拉取 /api/stats。
视觉：Kimi Work 看板 Hello World 风格（扁平无阴影、蓝白、六边形符号）。
费用：按 Kimi 开放平台刊例价估算（缓存命中/未命中/输出分别计价）。

数据来源：$KIMI_CODE_HOME/sessions（默认 ~/.kimi-code/sessions）下各会话
wire.jsonl 中 usageScope=="turn" 的 usage.record，含子 agent，不含 session
级汇总记录（避免重复计数）。

设置：默认从 ~/.kimi-code/kimi-board.json 读取；/settings 网页可视化配置
（会员档位 / 计费周期起止时分 / 价格来源 / 官方配额），无配置文件时用内置默认。

价格来源：
  - kimi（默认）：抓 platform.kimi.com/docs/pricing/*.md 官方刊例（元 / 1M）
  - modelsdev：抓 models.dev/api.json 的 moonshotai 组（USD / 1M，按汇率折元）
  - manual：仅手动价目 + 内置兜底
  手动 override（/settings 或配置文件中 pricing.overrides）优先级最高。

官方配额：5 小时限额 / 周限额来自 kimi-code 官方接口——优先走本地
kimi web 的 GET /api/v1/oauth/usage?provider=managed:kimi-code（server.token 认证），
失败则直连 https://api.kimi.com/coding/v1/usages（~/.kimi-code/credentials 的
OAuth token，必要时自动刷新）。窗口 used/limit/reset_at 均取官方数值。

用法：
  python kimi_board.py                      # 默认 127.0.0.1:8321
  python kimi_board.py --port 9000 --plan-price 199 --no-open
  python kimi_board.py --cycle-day 5 --cycle-hour 9 --cycle-minute 30
  python kimi_board.py --price-source modelsdev
"""

import argparse
import calendar
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def kimi_home() -> Path:
    """kimi code 数据根目录，尊重 KIMI_CODE_HOME 环境变量。"""
    env = os.environ.get("KIMI_CODE_HOME")
    return Path(env) if env else Path.home() / ".kimi-code"

USAGE_KEYS = ("inputOther", "inputCacheRead", "inputCacheCreation", "output")

# 内置兜底价目（元 / 1M tokens）：(缓存命中, 输入未命中, 输出)。
# 仅在没有联网、抓取失败且无手动覆盖时使用。
# https://platform.kimi.com/docs/pricing/chat-k3  ·  /docs/pricing/chat-k27-code
PRICING_FALLBACK = {
    "kimi-code/k3": (2.0, 20.0, 100.0),                     # kimi-k3
    "kimi-code/k3-256k": (2.0, 20.0, 100.0),                # k3 的 256k 变体，按 k3 价
    "kimi-code/kimi-for-coding-highspeed": (2.6, 13.0, 54.0),  # kimi-k2.7-code-highspeed
    "kimi-code/kimi-for-coding": (1.3, 6.5, 27.0),          # kimi-k2.7-code
}
DEFAULT_PRICE = (2.0, 20.0, 100.0)

# 订阅会员档位 -> 月付价格（元）
# https://www.kimi.com/zh-cn/resources/kimi-k3-pricing
PLAN_PRICES = {
    "adagio": 0.0,
    "andante": 49.0,
    "moderato": 99.0,
    "allegretto": 199.0,
    "allegro": 699.0,
}
DEFAULT_PLAN_PRICE = 199.0

# kimi-code 模型名 -> 官方平台商品名（定价页 / models.dev 里的名字）
KIMI_MODEL_MAP = {
    "kimi-code/k3": "kimi-k3",
    "kimi-code/k3-256k": "kimi-k3",          # k3-256k 与 k3 同模型仅上下文减半，按 k3 计价
    "kimi-code/kimi-for-coding-highspeed": "kimi-k2.7-code-highspeed",
    "kimi-code/kimi-for-coding": "kimi-k2.7-code",
}
# 官方定价页（Mintlify，加 .md 即 Markdown，价格在 DocTable 的 rows 里）
KIMI_DOCS_PAGES = (
    "https://platform.kimi.com/docs/pricing/chat-k3.md",
    "https://platform.kimi.com/docs/pricing/chat-k27-code.md",
)
# models.dev 价格（USD / 1M tokens）：data["moonshotai"]["models"][id]["cost"]
# cost = { "cache_read": 缓存命中, "input": 输入未命中, "output": 输出 }
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_PROVIDER = "moonshotai"
# 汇率接口（免 key），失败时回退到配置里的 usdCny 或 7.25
FX_RATE_URL = "https://open.er-api.com/v6/latest/USD"
DEFAULT_USD_CNY = 7.25

# 官方配额接口
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
KIMI_OAUTH_HOST = "https://auth.kimi.com"
KIMI_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"

# ---------------------------------------------------------------- 配置文件

CONFIG_FILE = "kimi-board.json"      # 位于 kimi_home() 下，网页设置页读写
CACHE_FILE = "kimi-board-cache.json"  # 价格/配额抓取结果的离线快照


def default_config() -> dict:
    return {
        "version": 1,
        "plan": {"auto": True, "tier": "", "price": None},
        "billing": {"day": 1, "hour": 0, "minute": 0},
        "pricing": {"source": "kimi", "usdCny": None, "overrides": {}, "k3half": False},
        "quota": {"enabled": True, "source": "auto"},
        "subscription": {"enabled": True, "source": "auto", "persistToken": False},
    }


def config_path() -> Path:
    return kimi_home() / CONFIG_FILE


def cache_path() -> Path:
    return kimi_home() / CACHE_FILE


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    p = config_path()
    if p.is_file():
        try:
            return _merge(default_config(), json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return default_config()


def save_config(cfg: dict) -> None:
    try:
        home = kimi_home()
        home.mkdir(parents=True, exist_ok=True)
        config_path().write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def load_cache() -> dict:
    p = cache_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(data: dict) -> None:
    try:
        home = kimi_home()
        home.mkdir(parents=True, exist_ok=True)
        cache_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


CFG = default_config()  # 运行时配置（配置文件 + CLI 覆盖合并后的结果）
SERVER_PORT = 8321       # 当前监听端口
_connect_queue = None    # "连接 Kimi" 命令队列，由主线程消费并开 WebView
_webview_active = False  # 后台 WebView 会话是否在运行
_webview_stop = False    # 请求后台 WebView 停止并清理登录态
_webview_dbg = {}        # WebView 内诊断信息（供 /api/debug 排查）

# ---- 本地接口防护：每次运行生成随机 secret，用于非白名单来源的写操作 ----
_local_secret = secrets.token_hex(16)
# 允许的本地来源（kimi web UI 端口段）——浏览器里恶意网页无法伪造 Origin
_LOCAL_ORIGIN_RE = re.compile(
    r"^http://(127\.0\.0\.1|localhost):(5862[7-9]|5863[0-9])$")
_manual_token = ""     # 手动 Token 只在内存；不回写配置文件
_sub_persist = False   # 是否已把 Token 存进系统凭据库（Windows）
_CRED_TARGET = "KimiBoard/KimiWebToken"


# ---------------------------------------------------------------- 价格

_PRICE_TABLE = dict(PRICING_FALLBACK)
_PRICE_META = {
    "source": "fallback", "currency": "CNY", "fetchedAt": 0,
    "ok": False, "message": "内置兜底价目（未联网抓取）",
}
_pricing_lock = threading.Lock()
_pricing_fetching = {"at": 0.0, "busy": False, "done": False}

_FX = {"rate": DEFAULT_USD_CNY, "at": 0.0}
_fx_lock = threading.Lock()


def _http_json(url: str, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "kimi-board")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_cny(text: str):
    """从官方定价页 Markdown 的 DocTable rows 里解析出 {平台模型名: (命中, 未命中, 输出)}。"""
    prices = {}
    for block in re.findall(r"rows=\{\[([\s\S]*?)\]\}", text):
        for row in re.findall(r"\[([\s\S]*?)\]", block):
            cells = re.findall(r'"([^"]*)"', row)
            if len(cells) < 5:
                continue
            num = []
            ok = True
            for cell in cells[2:5]:
                m = re.match(r"¥?\s*([\d.]+)", cell.strip())
                if not m:
                    ok = False
                    break
                num.append(float(m.group(1)))
            if ok:
                prices[cells[0]] = tuple(num)
    return prices


def fetch_kimi_docs_prices() -> tuple:
    """官方刊例价（元 / 1M）。返回 (model->(hit,miss,out), ok)。"""
    table = {}
    for url in KIMI_DOCS_PAGES:
        try:
            text = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "kimi-board"}), timeout=10
            ).read().decode("utf-8", errors="replace")
            table.update(_parse_cny(text))
        except Exception:
            continue
    return table, bool(table)


def fetch_modelsdev_prices() -> tuple:
    """models.dev 价格（USD / 1M）。返回 (model->(hit,miss,out), ok)。"""
    try:
        data = _http_json(MODELS_DEV_URL, timeout=10)
        models = (data.get(MODELS_DEV_PROVIDER) or {}).get("models") or {}
        table = {}
        for code_model, plat in KIMI_MODEL_MAP.items():
            cost = (models.get(plat) or {}).get("cost") or {}
            hit, miss, out = (
                cost.get("cache_read"), cost.get("input"), cost.get("output"),
            )
            if miss is None or out is None:
                continue
            if hit is None:  # 无缓存价时按未命中价兜底（保守）
                hit = miss
            table[code_model] = (float(hit), float(miss), float(out))
        return table, bool(table)
    except Exception:
        return {}, False


def _fetch_fx_rate() -> float:
    """USD->CNY 汇率，来自 open.er-api.com，失败返回 None。"""
    try:
        data = _http_json(FX_RATE_URL, timeout=6)
        rate = ((data.get("rates") or {}).get("CNY"))
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    except Exception:
        pass
    return None


def usd_cny_rate(cfg: dict) -> tuple:
    """返回 (rate, source)。配置里显式指定优先，否则自动抓取（带缓存）。"""
    manual = cfg["pricing"].get("usdCny")
    if manual:
        return float(manual), "manual"
    now = time.time()
    with _fx_lock:
        if _FX["at"] and now - _FX["at"] < 6 * 3600:
            return _FX["rate"], "auto"
    rate = _fetch_fx_rate()
    with _fx_lock:
        if rate:
            _FX.update(rate=rate, at=now)
            return rate, "auto"
        return _FX["rate"], "auto" if _FX["at"] else "default"


_plan_cache = {"at": 0.0, "result": None}


def kimi_instances():
    """读 kimi_home()/server/instances/*.json，按心跳时间倒序返回最近的实例。"""
    instances = []
    for p in (kimi_home() / "server" / "instances").glob("*.json"):
        try:
            instances.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    instances.sort(key=lambda i: -(i.get("heartbeat_at") or 0))
    return instances


def kimi_server_token() -> str:
    return (kimi_home() / "server.token").read_text(encoding="utf-8").strip()


def _kimi_local_call(inst, path: str, params: str = "", timeout=2.0):
    """对本地 kimi web 实例发 Bearer 请求，成功返回 data（信封内的业务数据）。"""
    token = kimi_server_token()
    url = f"http://{inst.get('host', '127.0.0.1')}:{inst['port']}{path}"
    if params:
        url += "?" + urllib.parse.quote(params, safe="=")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get("data")


def _kimi_local_any(path: str, params: str = "", timeout=2.0):
    """遍历本地实例，返回第一个成功的结果。"""
    for inst in kimi_instances()[:3]:
        try:
            result = _kimi_local_call(inst, path, params, timeout)
            if result is not None:
                return result
        except Exception:
            continue
    return None


def detect_plan():
    """从正在运行的 kimi web 实例读取会员档位，返回 (价格, 档位名)；失败返回 None。

    原理：kimi web 每个实例在 server/instances/ 注册 host/port，
    用 server.token 作为 bearer 调 /api/v1/oauth/userinfo 拿 userLevelName。
    成功结果缓存 10 分钟；失败只缓存 30 秒（避免 WebUI 短暂掉线后长时间回退默认价）。
    """
    now = time.time()
    ttl = 600 if _plan_cache["result"] else 30
    if _plan_cache["at"] and now - _plan_cache["at"] < ttl:
        return _plan_cache["result"]
    result = None
    try:
        data = _kimi_local_any("/api/v1/oauth/userinfo")
        name = ((data or {}).get("userInfo") or {}).get("userLevelName")
        price = PLAN_PRICES.get(name.lower()) if name else None
        if price is not None:
            result = (price, name)
    except Exception:
        pass
    _plan_cache.update(at=now, result=result)
    return result


def resolve_plan(cfg: dict):
    """合并配置文件 + 自动识别的会员档位，返回 (价格, 档位名, 是否自动, 来源说明)。

    优先级：config.plan.price 显式价 > config.plan.tier 档位价 > 自动识别 > 默认价。
    """
    plan = cfg["plan"]
    if plan.get("price") is not None:
        return float(plan["price"]), None, False, "custom"
    if plan.get("tier"):
        name = plan["tier"]
        return PLAN_PRICES.get(name, DEFAULT_PLAN_PRICE), name, False, "tier"
    if plan.get("auto"):
        auto = detect_plan()
        if auto:
            return auto[0], auto[1], True, "auto"
    return DEFAULT_PLAN_PRICE, None, False, "default"


# ---------------------------------------------------------------- 价格解析 & 配额

_PRICE_FETCH_TTL = 6 * 3600   # 成功 6h
_PRICE_FAIL_TTL = 600          # 失败 10min


def _raw_fetch(source: str, cfg: dict) -> tuple:
    """抓取来源价目表（元 / 1M）。返回 (table, meta)。"""
    if source == "modelsdev":
        table, ok = fetch_modelsdev_prices()
        if ok:
            rate, rate_src = usd_cny_rate(cfg)
            table = {m: tuple(v * rate for v in vals) for m, vals in table.items()}
            return table, {
                "ok": True, "currency": "CNY", "fetchedAt": int(time.time() * 1000),
                "message": f"models.dev (USD×{rate:.2f}, {rate_src})",
            }
        return {}, {"ok": False, "currency": "CNY", "fetchedAt": 0,
                    "message": "models.dev 抓取失败，回退内置价目"}
    # kimi 官方刊例
    table, ok = fetch_kimi_docs_prices()
    if ok:
        return table, {
            "ok": True, "currency": "CNY", "fetchedAt": int(time.time() * 1000),
            "message": "platform.kimi.com 官方刊例",
        }
    return {}, {"ok": False, "currency": "CNY", "fetchedAt": 0,
                "message": "官方定价页抓取失败，回退内置价目"}


def _to_code_names(table: dict) -> dict:
    """把价目表键统一为 kimi-code 模型名（兼容平台名 kimi-k3 等）。"""
    rev = {}
    for code_m, plat in KIMI_MODEL_MAP.items():
        rev.setdefault(plat, code_m)
    out = {}
    for k, v in table.items():
        if k in KIMI_MODEL_MAP:
            out[k] = v
        elif k in rev:
            out[rev[k]] = v
    return out


def _build_price_table(raw: dict, cfg: dict) -> dict:
    """由抓取结果(raw)+配置生成最终生效价目表。

    优先级：手动 override > 抓取结果 > 内置兜底；
    开启 k3half 时，kimi-code/k3-256k 一律按 k3 生效价的 50% 计算。
    """
    table = {}
    overrides = cfg["pricing"].get("overrides") or {}
    for model, fb in PRICING_FALLBACK.items():
        if model in overrides:
            table[model] = tuple(float(x) for x in overrides[model])
        elif model in raw:
            table[model] = raw[model]
        else:
            table[model] = fb
    for m, v in overrides.items():
        if m not in PRICING_FALLBACK:  # 自定义模型
            table[m] = tuple(float(x) for x in v)
    if cfg["pricing"].get("k3half") and "kimi-code/k3" in table:
        table["kimi-code/k3-256k"] = tuple(v * 0.5 for v in table["kimi-code/k3"])
    return table


def _seed_price_cache() -> None:
    """启动时用上次抓取快照预填价目，保证首屏即有正确价格。"""
    global _PRICE_TABLE, _PRICE_META
    prices = (load_cache().get("prices") or {})
    table = prices.get("table")
    if isinstance(table, dict) and table:
        cleaned = {}
        for k, v in table.items():
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                try:
                    cleaned[k] = (float(v[0]), float(v[1]), float(v[2]))
                except (TypeError, ValueError):
                    continue
        cleaned = _to_code_names(cleaned)
        if cleaned:
            _PRICE_TABLE = _build_price_table(cleaned, CFG)
            meta = dict(prices.get("meta") or {})
            meta.update(source=prices.get("source", "cache"),
                        message=(meta.get("message") or "") + "（上次快照，正在同步…）")
            _PRICE_META = meta


def refresh_pricing(force=False) -> None:
    """重建全局 _PRICE_TABLE / _PRICE_META（并发安全，重复请求合并）。"""
    global _PRICE_TABLE, _PRICE_META
    cfg = CFG
    now = time.time()
    with _pricing_lock:
        if not force and _pricing_fetching["done"] and \
                (now - _pricing_fetching["at"]) < (_PRICE_FETCH_TTL if _PRICE_META["ok"] else _PRICE_FAIL_TTL):
            return
        if _pricing_fetching["busy"]:
            # 等待正在进行的抓取完成（最多 8s），避免首屏用兜底价
            deadline = now + 8
            while _pricing_fetching["busy"] and time.time() < deadline:
                time.sleep(0.1)
            return
        _pricing_fetching["busy"] = True

    table, raw, meta = {}, {}, {"ok": False, "currency": "CNY", "fetchedAt": 0, "message": ""}
    try:
        source = cfg["pricing"]["source"]
        if source != "manual":
            raw, meta = _raw_fetch(source, cfg)
            if not raw:
                # 离线回退：使用上次抓取成功的快照
                cached = (load_cache().get("prices") or {})
                if cached.get("source") == source and isinstance(cached.get("table"), dict):
                    raw = cached["table"]
                    meta = dict(cached.get("meta") or {}, message=meta["message"] + "（离线用上次快照）")
            # 快照落盘，离线可用（存原始平台名版本，加载时统一转换）
            cache = load_cache()
            cache.setdefault("prices", {}).update(
                source=source, table=raw, meta=meta, savedAt=int(time.time() * 1000))
            save_cache(cache)
        else:
            meta = {"ok": False, "currency": "CNY", "fetchedAt": 0,
                    "message": "手动价目模式（仅内置 + override）"}
        meta = dict(meta, source=source)
        raw = _to_code_names(raw)
        table = _build_price_table(raw, cfg)
        if not meta["ok"] and (cfg["pricing"].get("overrides") or {}):
            meta["message"] = "手动 override 生效，其余模型使用内置价目"
    finally:
        _PRICE_TABLE = table
        _PRICE_META = meta
        with _pricing_lock:
            _pricing_fetching.update(at=now, busy=False, done=True)


def price_of(model: str):
    """查模型单价；容忍日志里不带 kimi-code/ 前缀的写法。"""
    return (_PRICE_TABLE.get(model)
            or _PRICE_TABLE.get(f"kimi-code/{model}")
            or DEFAULT_PRICE)


def pricing_info() -> dict:
    return dict(_PRICE_META, table={m: list(v) for m, v in _PRICE_TABLE.items()})


# ---- 官方配额：5 小时 / 周 限额 ----

_quota_cache = {"at": 0.0, "data": None, "busy": False}
_quota_lock = threading.Lock()
_QUOTA_TTL = 30.0  # 配额数据 30 秒内视为新鲜


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_reset_at(v):
    if not isinstance(v, str) or not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_rows(rows: list) -> list:
    """把 (name, window(duration,unit), used, limit, resetAt) 归一成统一结构。"""
    out = []
    for name, window, used, limit, reset_at in rows:
        if limit <= 0:
            continue
        row = {"name": name, "window": window, "used": used, "limit": limit,
               "resetAt": reset_at, "pct": round(used / limit * 100, 1)}
        # 推算：以窗口内平均速率估计触顶时间（仅在窗口重置前可触顶时给出）
        window_sec = None
        if window and window[1] == "hour":
            window_sec = window[0] * 3600
        elif window and window[1] == "week":
            window_sec = window[0] * 7 * 86400
        used_pct = used / limit * 100
        est = _estimate_eta(used_pct, reset_at, window_sec,
                            datetime.now().astimezone()) if window_sec else None
        if est:
            row["etaSeconds"] = est["etaSeconds"]
            row["willHit"] = est["willHit"]
            row["resetIn"] = est["resetIn"]
        out.append(row)
    return out


def fetch_quota_local() -> tuple:
    """本地 kimi web：GET /api/v1/oauth/usage?provider=managed:kimi-code。"""
    data = _kimi_local_any(
        "/api/v1/oauth/usage",
        params="provider=managed:kimi-code",
        timeout=2.5,
    )
    if not data:
        return None, "本地 kimi web 未运行或接口不可用"
    rows = []
    summary = data.get("summary")
    if summary:
        w = summary.get("window") or {}
        rows.append((summary.get("name", "Weekly limit"), (w.get("duration", 1), w.get("unit", "week")),
                     _to_int(summary.get("used")), _to_int(summary.get("limit")), summary.get("reset_at")))
    for lim in data.get("limits") or []:
        w = lim.get("window") or {}
        name = lim.get("name")
        if not name:
            name = {("hour", 5): "5h limit", ("hour", 24): "24h limit",
                    ("minute", 300): "5h limit", ("day", 7): "Weekly limit"}.get(
                        (w.get("unit"), w.get("duration")), "limit")
        rows.append((name, (w.get("duration", 1), w.get("unit", "")),
                     _to_int(lim.get("used")), _to_int(lim.get("limit")), lim.get("reset_at")))
    return _normalize_rows(rows), None


def _cloud_access_token() -> tuple:
    """读 OAuth credentials，必要时刷新，返回 (token, error)。"""
    cred = kimi_home() / "credentials" / "kimi-code.json"
    if not cred.is_file():
        return None, "未找到 ~/.kimi-code/credentials/kimi-code.json（请先 kimi login）"
    try:
        info = json.loads(cred.read_text(encoding="utf-8"))
    except Exception:
        return None, "credentials 文件损坏"
    now = time.time()
    if _to_int(info.get("expires_at")) > now + 60:
        return info.get("access_token"), None
    # 需要刷新
    refresh_token = info.get("refresh_token")
    if not refresh_token:
        return None, "OAuth token 已过期且无 refresh_token"
    body = urllib.parse.urlencode({
        "client_id": KIMI_OAUTH_CLIENT_ID,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{KIMI_OAUTH_HOST}/api/oauth/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        access = payload.get("access_token")
        if not access:
            return None, "OAuth 刷新失败：响应缺少 access_token"
        info["access_token"] = access
        info["refresh_token"] = payload.get("refresh_token", refresh_token)
        info["expires_at"] = int(time.time()) + _to_int(payload.get("expires_in"), 3600)
        try:
            cred.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return access, None
    except Exception as e:
        return None, f"OAuth 刷新失败：{e}"


def fetch_quota_cloud() -> tuple:
    """云端直连：GET {KIMI_CODE_BASE_URL}/usages。"""
    token, err = _cloud_access_token()
    if not token:
        return None, err
    try:
        payload = _http_json(f"{KIMI_CODE_BASE_URL}/usages",
                             headers={"Authorization": f"Bearer {token}"}, timeout=8)
    except Exception as e:
        return None, f"云端配额接口请求失败：{e}"
    rows = []
    usage = payload.get("usage")
    if usage:
        rows.append(("Weekly limit", (1, "week"),
                     _to_int(usage.get("used")), _to_int(usage.get("limit")), usage.get("resetTime")))
    unit_map = {"TIME_UNIT_MINUTE": "minute", "TIME_UNIT_HOUR": "hour",
                "TIME_UNIT_DAY": "day", "TIME_UNIT_WEEK": "week"}
    for lim in payload.get("limits") or []:
        w = lim.get("window") or {}
        unit = unit_map.get(w.get("timeUnit"), "minute")
        dur = _to_int(w.get("duration"))
        if unit == "minute" and dur >= 60 and dur % 60 == 0:
            unit, dur = "hour", dur // 60
        name = lim.get("name") or {("hour", 5): "5h limit", ("hour", 24): "24h limit",
                                   ("week", 1): "Weekly limit"}.get((unit, dur), "limit")
        d = lim.get("detail") or {}
        rows.append((name, (dur, unit), _to_int(d.get("used")), _to_int(d.get("limit")),
                     d.get("resetTime")))
    return _normalize_rows(rows), None


def fetch_quota(cfg: dict, force=False) -> dict:
    """按配置同步官方配额。优先本地，再云端。结果 30s 缓存。"""
    now = time.time()
    with _quota_lock:
        if not force and _quota_cache["data"] is not None and now - _quota_cache["at"] < _QUOTA_TTL:
            return _quota_cache["data"]
        if _quota_cache["busy"]:
            return _quota_cache["data"] or {"ok": False, "message": "同步中…", "rows": []}
        _quota_cache["busy"] = True
    result = None
    try:
        source_cfg = cfg["quota"].get("source", "auto")
        rows, err, used_source = None, None, None
        if source_cfg in ("auto", "local"):
            rows, err = fetch_quota_local()
            if rows is not None:
                used_source = "local"
        if rows is None and source_cfg in ("auto", "cloud"):
            rows, err = fetch_quota_cloud()
            if rows is not None:
                used_source = "cloud"
        if rows is not None:
            result = {"ok": True, "message": None, "source": used_source,
                      "fetchedAt": int(now * 1000), "rows": rows}
        else:
            # 离线回退：使用上次抓取成功的快照
            cached = (load_cache().get("quota") or {})
            if cached.get("ok") and isinstance(cached.get("rows"), list):
                result = dict(cached, source=used_source or source_cfg,
                              message="离线快照（当前无法联网同步）",
                              fetchedAt=cached.get("fetchedAt", int(now * 1000)))
            else:
                result = {"ok": False, "message": err or "配额接口不可用",
                          "source": source_cfg, "fetchedAt": int(now * 1000), "rows": []}
        cache = load_cache()
        cache["quota"] = result
        save_cache(cache)
    finally:
        with _quota_lock:
            _quota_cache.update(at=now, data=result, busy=False)
    return result


def quota_snapshot(cfg: dict, force=False) -> dict:
    """供 /api/stats 与 /api/quota 使用：未启用则返回禁用态。"""
    if not cfg["quota"].get("enabled", True):
        return {"ok": False, "enabled": False, "message": "配额同步已关闭", "rows": [], "fetchedAt": 0}
    data = fetch_quota(cfg, force=force)
    data = dict(data, enabled=True)
    if data.get("ok") and not force:
        # 复用快照，若缓存新鲜则原样返回
        return data
    return data


# ---- 月额度（官网 GetSubscriptionStats）----
# 独立 adapter：KimiWebProvider → normalize_subscription → Board。
# 后端只保存归一化结果（比例 / 重置时间 / 提示），绝不保存 JWT/Cookie。
# 来源三选一：
#   auto   = 内置 WebView / 浏览器扩展把官网数据推来（POST /api/subscription）
#   manual = 设置页粘贴官网 JWT（高级/救援），看板自己调 GetSubscriptionStats
SUBSTATS_URL = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
SUBSTATS_MANUAL_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.kimi.com",
    "Referer": "https://www.kimi.com/membership/subscription?tab=quota",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "x-msh-platform": "web",
    "x-msh-version": "2.0.0",
}
_websub = {"at": 0.0, "data": None}  # 内存态，webview/extension 推送后更新（已归一化）

# 近期速率上下文：collect_stats 每次扫描后写入，integrated_limits 读取。
# 键: recent15/30/60 = 近 15/30/60 分钟 token 总量；monthTotal = 本周期 token 总量；
#     5hTotal / 7dTotal = 近 5h / 7d 滚动窗口 token 总量（按自然时间窗口近似）。
_rate_ctx = {"recent15": 0, "recent30": 0, "recent60": 0,
             "monthTotal": 0, "h5Total": 0, "d7Total": 0}

# WebView2 专属持久 profile：Cookie/JWT/localStorage 都留在这里，随 KIMI_CODE_HOME 走。
# 必须 private_mode=False 才会真正落盘（pywebview 默认 private_mode=True 用临时目录，不持久）。
WEBVIEW_PROFILE_DIR = kimi_home() / "webview-profile"

# 浏览器标签页 favicon：复用扩展图标（32px，HiDPI 下也清晰）。
# 运行时读取一次，读不到就留空（页面仍可正常加载，只是没有图标）。
def _load_favicon():
    for name in ("32.png", "16.png", "48.png"):
        p = Path(__file__).resolve().parent / "extension" / "icons" / name
        try:
            if p.exists():
                return p.read_bytes()
        except Exception:
            pass
    return None


FAVICON_BYTES = _load_favicon()


def _http_post_json(url: str, body: bytes, headers=None, timeout=8):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    req.add_header("User-Agent", "kimi-board")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_subscription(raw):
    """KimiWebProvider → Board：只抽取需要的字段，丢弃其余一切（含可能的用户 ID）。"""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(payload, dict) or not (
            payload.get("subscriptionBalance") or payload.get("ratelimitCode5h")):
        return None
    sub = payload.get("subscriptionBalance") or {}
    r5 = payload.get("ratelimitCode5h") or {}
    r7 = payload.get("ratelimitCode7d") or {}
    notice = payload.get("notice") or {}
    user = payload.get("user") or {}
    return {
        "amountUsedRatio": _fnum(sub.get("amountUsedRatio")),
        "kimiCodeUsedRatio": _fnum(sub.get("kimiCodeUsedRatio")),
        "expireTime": sub.get("expireTime"),
        "planLevel": (user.get("membership") or {}).get("level"),
        "limits5h": {
            "ratio": _fnum(r5.get("ratio")),
            "enabled": bool(r5.get("enabled", True)),
            "resetTime": r5.get("resetTime"),
        },
        "limits7d": {
            "ratio": _fnum(r7.get("ratio")),
            "enabled": bool(r7.get("enabled", True)),
            "resetTime": r7.get("resetTime"),
        },
        "notice": {
            "tip": notice.get("tip"),
            "content": notice.get("content"),
            "resetTime": notice.get("resetTime"),
        },
    }


# ---- Windows 凭据库（ctypes 直调 Credential Manager，零依赖）----

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _win_cred_write(target: str, value: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class CREDW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        blob = value.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob)
        cred = CREDW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False


def _win_cred_read(target: str):
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class CREDW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        cred = ctypes.c_void_p()
        if not ctypes.windll.advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred)):
            return None
        try:
            p = ctypes.cast(cred, ctypes.POINTER(CREDW)).contents
            return ctypes.string_at(p.CredentialBlob, p.CredentialBlobSize).decode("utf-16-le")
        finally:
            ctypes.windll.advapi32.CredFree(cred)
    except Exception:
        return None


def _win_cred_delete(target: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
    except Exception:
        pass


def set_manual_token(token: str, persist: bool) -> None:
    """手动 Token 只在内存；仅当 persist 时写入系统凭据库，从不写配置文件。"""
    global _manual_token, _sub_persist
    _manual_token = (token or "").strip().lstrip("\ufeff")
    _sub_persist = bool(persist)
    if _manual_token and persist:
        _win_cred_write(_CRED_TARGET, _manual_token)
    elif not _manual_token:
        _win_cred_delete(_CRED_TARGET)
        _sub_persist = False


def get_manual_token(cfg: dict):
    """取手动 Token：内存优先，其次系统凭据库。"""
    global _manual_token
    if _manual_token:
        return _manual_token
    if (cfg.get("subscription") or {}).get("persistToken"):
        tok = _win_cred_read(_CRED_TARGET)
        if tok:
            _manual_token = tok
            return tok
    return None


def fetch_subscription_manual(cfg: dict) -> tuple:
    """Provider：用官网 JWT 直接请求 GetSubscriptionStats，返回归一化结果。"""
    token = get_manual_token(cfg)
    if not token:
        return None, "未设置官网 Token（高级/救援模式需在设置页粘贴）"
    headers = dict(SUBSTATS_MANUAL_HEADERS, Authorization="Bearer " + token)
    try:
        payload = _http_post_json(SUBSTATS_URL, b"{}", headers)
    except Exception as e:
        return None, f"请求失败：{e}"
    norm = normalize_subscription(payload)
    if norm is None:
        return None, "返回结构不是 GetSubscriptionStats（接口可能已改版，请更新看板）"
    return norm, None


def _store_subscription(norm: dict, source: str) -> None:
    """归一化结果落缓存 + 内存（token 永不过到这里）。"""
    cache = load_cache()
    cache["subscription"] = {
        "data": norm, "fetchedAt": int(time.time() * 1000), "source": source,
    }
    save_cache(cache)
    _websub.update(at=time.time(), data=cache["subscription"])


def handle_subscription_post(raw, source: str) -> dict:
    """WebView / 扩展推送：归一化后仅存结果，不碰凭据。"""
    norm = normalize_subscription(raw)
    if norm is None:
        return {"ok": False, "error": "不是 GetSubscriptionStats 的返回结构"}
    _store_subscription(norm, source)
    return {"ok": True}


def _get_cookies_timeout(window, timeout=6):
    """带超时地读 WebView cookie，防止 UI 线程调度不到导致永久阻塞。"""
    box = {"ok": False, "cookies": [], "err": ""}

    def _run():
        try:
            box["cookies"] = window.get_cookies() or []
            box["ok"] = True
        except Exception as e:
            box["err"] = str(e)[:120]

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        box["err"] = "timeout"
    return box


def _cookies_for_url(window, url, timeout=6):
    """绕过 get_cookies() 的"当前 URL"限制，按指定 URL 查 CookieManager。

    kimi-auth 是 HttpOnly + host-only（无 Domain），get_cookies() 内部用 self.url，
    页面若不在 www.kimi.com 就会漏掉它。这里临时把 EdgeChromium.url 指到目标域，
    复用 pywebview 现成的 UI 线程调度实现（EdgeChromium.get_cookies），完事再恢复。
    返回 {"cookies": [(name, value)...], "err": str}。
    """
    box = {"cookies": [], "err": ""}

    def _run():
        try:
            from webview.platforms.winforms import BrowserView
            from System import Func, Type
            from threading import Semaphore
            # 按 uid 精确匹配当前窗口的 BrowserForm，取不到再退而取第一个
            uid = getattr(window, "uid", None)
            inst = None
            if uid is not None and uid in BrowserView.instances:
                inst = BrowserView.instances[uid]
            if inst is None:
                for w in BrowserView.instances.values():
                    inst = w
                    break
            edge = getattr(inst, "browser", None) if inst is not None else None
            if edge is None or not hasattr(edge, "get_cookies"):
                box["err"] = "no edge browser"
                return
            cookies, sem = [], Semaphore(0)

            def _do():
                old = getattr(edge, "url", None)
                try:
                    edge.url = url
                    edge.get_cookies(cookies, sem)  # 内部 ContinueWith 回 UI 线程后 release
                except Exception as e:
                    box["err"] = str(e)[:120]
                    try:
                        sem.release()
                    except Exception:
                        pass
                finally:
                    try:
                        edge.url = old
                    except Exception:
                        pass

            inst.Invoke(Func[Type](_do))
            sem.acquire()  # 等 UI 线程回调 release
            for c in cookies:
                try:
                    for morsel in c.values():
                        box["cookies"].append((morsel.key, morsel.value))
                except Exception:
                    continue
        except Exception as e:
            box["err"] = str(e)[:120]

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        box["err"] = "timeout"
    return box


def _fetch_via_cookie(window) -> dict:
    """从 WebView 读 kimi-auth cookie（含 HttpOnly），在内存里用它请求 API。

    JWT 仅作为本地变量用于本次请求，绝不写入配置/日志/缓存。
    """
    token = None
    names = []
    # 快路径：按当前页 URL 取（get_cookies 内部用 self.url）
    box = _get_cookies_timeout(window)
    for cookie in box["cookies"]:
        try:
            for morsel in cookie.values():
                names.append(morsel.key)
                if morsel.key == "kimi-auth" and morsel.value:
                    token = morsel.value
                    break
        except Exception:
            continue
        if token:
            break
    if box["err"]:
        _webview_dbg["cookieErr"] = box["err"]
    # 慢路径：当前 URL 没取到 kimi-auth → 显式按各候选域查 CookieManager
    # （kimi-auth 是 host-only，页面若不在 www.kimi.com 会被 get_cookies 过滤）
    if not token:
        for u in ("https://www.kimi.com", "https://kimi.com"):
            sub = _cookies_for_url(window, u)
            if sub["err"]:
                _webview_dbg["cookieErr2"] = f"{u}: {sub['err']}"
            for nm, val in sub["cookies"]:
                if nm not in names:
                    names.append(nm)
                if nm == "kimi-auth" and val:
                    token = val
            if token:
                _webview_dbg["cookieSrc"] = u
                break
    # 只记录 cookie 名字，绝不记录值（JWT 不进日志/Agent 上下文）
    _webview_dbg["cookieNames"] = names[-20:]
    _webview_dbg["hasKimiAuth"] = bool(token)
    if not token:
        return None
    try:
        headers = dict(SUBSTATS_MANUAL_HEADERS, Authorization="Bearer " + token)
        payload = _http_post_json(SUBSTATS_URL, b"{}", headers)
    except Exception as e:
        _webview_dbg["apiErr"] = str(e)[:120]
        return None
    return normalize_subscription(payload)


def _load_websub():
    if _websub["data"] is None:
        cache = load_cache()
        cached = cache.get("subscription") or {}
        if cached.get("data"):
            _websub.update(at=0.0, data=cached)
    return _websub["data"]


def subscription_snapshot(cfg: dict, force=False) -> dict:
    """返回月额度信息：{ok, enabled, source, fetchedAt, data, message}。"""
    sub = cfg.get("subscription") or {}
    if not sub.get("enabled", True):
        return {"ok": False, "enabled": False, "message": "月额度同步已关闭", "data": None}
    if sub.get("source") == "manual" and get_manual_token(cfg):
        cached = _load_websub()
        fresh = cached and (time.time() - cached.get("fetchedAt", 0) / 1000) < 300
        if force or not fresh:
            norm, err = fetch_subscription_manual(cfg)
            if norm:
                cached = {"data": norm, "fetchedAt": int(time.time() * 1000), "source": "manual"}
                cache = load_cache(); cache["subscription"] = cached; save_cache(cache)
                _websub.update(at=time.time(), data=cached)
            elif not fresh and cached:
                cached = dict(cached, message="手动同步失败：" + (err or ""))
    else:
        cached = _load_websub()
    if not cached or not cached.get("data"):
        return {"ok": False, "enabled": True,
                "message": "尚无月额度数据：在设置页「连接 Kimi」登录，或安装浏览器扩展自动同步",
                "data": None}
    return {"ok": True, "enabled": True,
            "source": cached.get("source", "auto"),
            "fetchedAt": cached.get("fetchedAt", 0),
            "message": cached.get("message") or "",
            "data": cached.get("data")}


def _mark_session(active: bool) -> None:
    """记录 WebView 持久会话标记（只存布尔，不含任何凭据）。"""
    cache = load_cache()
    cache.setdefault("webviewSession", {})
    cache["webviewSession"]["active"] = bool(active)
    cache["webviewSession"]["at"] = int(time.time() * 1000)
    save_cache(cache)


def session_active() -> bool:
    return bool((load_cache().get("webviewSession") or {}).get("active"))


def run_connect_webview(port: int) -> None:
    """主线程内运行：登录窗口（WebView2 持久 profile，会话留存）。

    登录成功 → 记会话标记 → 隐藏窗口，每 30s 用持久会话后台刷新 GetSubscriptionStats；
    连续失败（会话失效）→ 弹窗让用户重新登录；退出/清除时清 kimi.com 登录态。
    """
    global _webview_active, _webview_stop
    import time as _t
    try:
        import webview
    except ImportError:
        return
    _webview_active = True
    _webview_stop = False
    probe = r"""(function(){
      var U='%s';
      function capture(o){
        if(o&&(o.subscriptionBalance||o.ratelimitCode5h)) window.__kb_sub={ok:true,data:JSON.stringify(o)};
      }
      // 1) 钩子：站点自己请求 GetSubscriptionStats 时，捕获其响应（和请求头）
      if(!window.__kb_hooked){
        window.__kb_hooked=true; window.__kb_hdrs=null; window.__kb_apifail=false;
        try{
          var of=window.fetch;
          window.fetch=function(url,opts){
            var u=typeof url==='string'?url:(url&&url.url);
            if(u&&u.indexOf('GetSubscriptionStats')!==-1){
              if(opts&&opts.headers){
                try{var h=opts.headers,o={};
                  if(h&&typeof h.forEach==='function'){h.forEach(function(v,k){o[k]=v;});}
                  else{for(var k in h){o[k]=h[k];}}
                  window.__kb_hdrs=o;}catch(e){}
              }
              var p=of.apply(this,arguments);
              p.then(function(r){r.clone().text().then(function(t){try{capture(JSON.parse(t));}catch(e){}}).catch(function(){});}).catch(function(){});
              return p;
            }
            return of.apply(this,arguments);
          };
        }catch(e){}
        try{
          var ox=XMLHttpRequest.prototype.open,os=XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open=function(m,u){this.__kb_url=u;return ox.apply(this,arguments);};
          XMLHttpRequest.prototype.send=function(){
            var x=this;
            this.addEventListener('load',function(){
              try{if(x.__kb_url&&x.__kb_url.indexOf('GetSubscriptionStats')!==-1){capture(JSON.parse(x.responseText));}}catch(e){}
            });
            return os.apply(this,arguments);
          };
        }catch(e){}
      }
      if(window.__kb_sub){
        var __r=JSON.stringify(window.__kb_sub);
        window.__kb_sub=null; // 已消费，下一轮重新拉取，保证后台实时刷新拿到新数据
        return __r;
      }
      // 主路径：selfFetch 调 API（含 Kimi/KimiCode 拆分字段），认证头从 account token 派生
      if(!window.__kb_pending){ selfFetch(function(){}); }
      // 兜底：DOM 抓取（页面已渲染的数字）。token 过期后 API 持续 401，DOM 是可持续数据源。
      // 只要 DOM 有数据就用（不被 pending 永久阻塞；API 成功时 capture 会用完整数据覆盖）
      try{
        var d=scrapeDom();
        if(d && (d.subscriptionBalance.amountUsedRatio!=null)){
          btnState(true);
          // API 在途且未失败时，先等 API（拿拆分字段）；API 已失败则直接用 DOM
          if(!window.__kb_apifail && window.__kb_pending){ /* 等 API */ }
          else { return JSON.stringify({ok:true,data:JSON.stringify(d)}); }
        }
      }catch(e){}
      // 兜底按钮
      if(!document.getElementById('kb-sync-done')){
        var b=document.createElement('div');
        b.id='kb-sync-done';
        b.style.cssText='position:fixed;z-index:2147483647;left:16px;top:16px;background:#8b96a8;color:#fff;border:0;border-radius:10px;padding:10px 16px;font:600 13px/1 "PingFang SC",system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 14px rgba(20,40,80,.25);user-select:none';
        b.textContent='\u5c1a\u672a\u767b\u5f55\uff0c\u8bf7\u5148\u767b\u5f55';
        b.onclick=function(){
          b.textContent='\u68c0\u6d4b\u4e2d...';
          selfFetch(function(ok,msg){ btnState(ok); });
        };
        try{document.body.appendChild(b);}catch(e){}
      }
      window.__kb_dbg={
        err:window.__kb_lasterr||'',
        href:(location.href||'').slice(0,90),
        apifail:window.__kb_apifail?1:0,
        pending:window.__kb_pending?1:0,
        // 是否找到 account token（仅布尔，不暴露 token 本身）
        authFound:(function(){var a=accountAuth();return a?'yes':'no';})(),
        // 总量 Code 段宽度（调试用，仅数字）
        codeRatio:(function(){var v=(typeof codeRatioFromBar==='function')?codeRatioFromBar():null;return v!=null?v:'none';})()
      };
      return null;
      // ---- helpers ----
      function btnState(ok){
        var b=document.getElementById('kb-sync-done');
        if(!b) return;
        b.style.background = ok ? '#2e6fe8' : '#8b96a8';
        b.textContent = ok ? '\u2713 \u5df2\u767b\u5f55\uff0c\u540c\u6b65\u5e76\u540e\u53f0\u5237\u65b0' : '\u5c1a\u672a\u767b\u5f55\uff0c\u8bf7\u5148\u767b\u5f55';
      }
      function findJwt(){
        var jt=null;
        try{
          var cs=document.cookie.split(';');
          for(var i=0;i<cs.length;i++){var p=cs[i].trim();if(p.indexOf('kimi-auth=')===0){jt=p.slice(10);break;}}
        }catch(e){}
        if(!jt){
          try{
            var stores=[localStorage,sessionStorage];
            for(var s=0;s<stores.length&&!jt;s++){
              for(var i=0;i<stores[s].length;i++){
                var v=stores[s].getItem(stores[s].key(i));
                if(!v) continue;
                if(v.indexOf('eyJ')===0){ jt=v; break; }
                var m=v.match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
                if(m){ jt=m[0]; break; }
              }
            }
          }catch(e){}
        }
        return jt;
      }
      // 解析 JWT payload（base64url），提取派生头所需的字段；token 只在页面内用，不外泄
      function parseJwt(tk){
        try{
          var p=tk.split('.')[1].replace(/-/g,'+').replace(/_/g,'/');
          while(p.length%%4)p+='=';
          return JSON.parse(decodeURIComponent(escape(atob(p))));
        }catch(e){return null;}
      }
      // 在 localStorage/sessionStorage 里找 account token（iss==='account'，含 device_id/ssid/sub）
      // 接口认证用的是它（不是 cookie 里的 user-center kimi-auth）
      function accountAuth(){
        var cands=[];
        try{
          var stores=[localStorage,sessionStorage];
          for(var s=0;s<stores.length;s++){
            for(var i=0;i<stores[s].length;i++){
              var v=stores[s].getItem(stores[s].key(i));
              if(!v) continue;
              var m=v.match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
              var tk=(v.indexOf('eyJ')===0)?v:(m?m[0]:null);
              if(tk) cands.push(tk);
            }
          }
        }catch(e){}
        // 也看 cookie 里的 token（作为候选，但 account token 优先）
        try{
          var cs=document.cookie.split(';');
          for(var i=0;i<cs.length;i++){var p=cs[i].trim();
            if(/^kimi-auth=/.test(p)){cands.push(p.slice(10));}}
        }catch(e){}
        for(var c=0;c<cands.length;c++){
          var pl=parseJwt(cands[c]);
          if(pl&&pl.iss==='account'&&pl.device_id&&pl.ssid){
            return {
              'Authorization':'Bearer '+cands[c],
              'x-msh-device-id':String(pl.device_id),
              'x-msh-session-id':String(pl.ssid),
              'x-traffic-id':String(pl.sub||'')
            };
          }
        }
        return null;
      }
      function selfFetch(cb){
        if(window.__kb_pending){ cb&&cb(false,'busy'); return; }
        window.__kb_pending=true;
        // 静态头 + connect 协议版本（接口要求，缺了会 401）
        var hdrs={'Content-Type':'application/json','connect-protocol-version':'1',
                  'x-msh-platform':'web','x-msh-version':'2.0.0','x-language':'zh-CN'};
        // 复用站点自己请求时捕获到的请求头（最权威），覆盖默认
        try{if(window.__kb_hdrs){for(var k in window.__kb_hdrs){hdrs[k]=window.__kb_hdrs[k];}}}catch(e){}
        // 认证：从 account token 派生 Authorization + x-msh-device-id/session-id/x-traffic-id
        if(!hdrs['authorization']&&!hdrs['Authorization']){
          var au=accountAuth();
          if(au){for(var k2 in au){hdrs[k2]=au[k2];}}
        }
        // 诊断：记录实际发送的头键名（不记值，防泄密）
        window.__kb_sentHdrs=Object.keys(hdrs).sort();
        // 超时保护：8s 未响应则中止，防止 __kb_pending 卡死
        var ctrl=(window.AbortController?new AbortController():null);
        var sig=ctrl?ctrl.signal:undefined;
        var to=setTimeout(function(){try{ctrl&&ctrl.abort();}catch(e){}},8000);
        (function(){
          fetch(U,{method:'POST',headers:hdrs,body:'{}',credentials:'include',signal:sig})
            .then(function(r){clearTimeout(to);window.__kb_pending=false;window.__kb_lasterr='HTTP '+r.status;return r.ok?r.text():Promise.reject('HTTP '+r.status);})
            .then(function(t){try{var o=JSON.parse(t);capture(o);window.__kb_apifail=false;btnState(true);cb&&cb(true,'ok');}catch(e){window.__kb_apifail=true;window.__kb_lasterr='parse:'+e;btnState(false);cb&&cb(false,'parse');}})
            .catch(function(e){clearTimeout(to);window.__kb_sub=null;window.__kb_pending=false;window.__kb_apifail=true;window.__kb_lasterr=''+e;btnState(false);cb&&cb(false,String(e));});
        })();
      }
      function numIn(src,re){var m=src.match(re);return m?parseFloat(m[1]):null;}
      function seg(a,b){var i=document.body.innerText.indexOf(a),j=document.body.innerText.indexOf(b);
        return (i>=0&&j>i)?document.body.innerText.slice(i,j):'';}
      // 从元素读宽度百分比：el.style.width 形如 "64.69 百分比"，getAttribute('style') 形如 "width: 64.69 百分比;"
      function widthPct(el){
        if(!el) return null;
        var w=(el.style&&el.style.width)||'';
        var m=w.match(/([\d.]+)/);           // 提取小数部分
        if(m) return parseFloat(m[1]);
        var s=el.getAttribute('style')||'';
        var m2=s.match(/width:\s*([\d.]+)%%/);
        return m2?parseFloat(m2[1]):null;
      }
      // Code 段宽度：在"总使用量"所在的 usage-section 里，找 .kimi-progress 的 .blue 段
      // （DOM 常驻，无需 hover；.primary 是 Kimi 段，.blue 是 Code 段，width 即占月额度比例）
      function codeRatioFromBar(){
        try{
          var secs=document.querySelectorAll('.usage-section');
          for(var i=0;i<secs.length;i++){
            var s=secs[i];
            var title=s.querySelector('.usage-section-title');
            var tt=(title?title.textContent:'')||s.textContent||'';
            if(tt.indexOf('\u603b\u4f7f\u7528\u91cf')===-1) continue;  // 只认"总使用量"区
            var blue=s.querySelector('.kimi-progress .blue');
            var v=widthPct(blue);
            if(v!=null) return v;
          }
          // 兜底：任意 .kimi-progress 里同时有 .primary 和 .blue 的，取第一个 .blue
          var bars=document.querySelectorAll('.kimi-progress');
          for(var j=0;j<bars.length;j++){
            if(bars[j].querySelector('.primary')&&bars[j].querySelector('.blue')){
              var v2=widthPct(bars[j].querySelector('.blue'));
              if(v2!=null) return v2;
            }
          }
        }catch(e){}
        return null;
      }
      function scrapeDom(){
        var t=document.body?document.body.innerText:'';
        // tooltip 可能是隐藏元素，innerText 读不到 → 用 textContent 兜底（含隐藏节点）
        var tc=document.body?document.body.textContent:'';
        // 总使用量，形如 "总使用量 91.97 百分比"
        var used=numIn(t,/\u603b\u4f7f\u7528\u91cf[^\d]*([\d.]+)%%/);
        if(used==null) used=numIn(tc,/\u603b\u4f7f\u7528\u91cf[^\d]*([\d.]+)%%/);
        // 5 小时用量 / 7 天用量：分段取 Code 后面的百分比数字
        var h5s=seg('\u5c0f\u65f6\u7528\u91cf','\u5929\u7528\u91cf');        // 小时用量 ~ 天用量
        var d7s=seg('\u5929\u7528\u91cf','\u989d\u5ea6\u52a0\u6cb9\u5305');  // 天用量 ~ 额度加油包
        var h5=numIn(h5s,/Code[^\d]*([\d.]+)%%/i);
        var d7=numIn(d7s,/Code[^\d]*([\d.]+)%%/i);
        // Kimi Code 拆分：直接读总量进度条 .blue 段的 style.width（占月额度的比例）
        var code=codeRatioFromBar();
        // 重置时间：优先取 "08-11 23:22 后重置" 里的日期时间
        var r5=/(\d{2}-\d{2})\s*(\d{2}:\d{2})/.exec(h5s);
        var r7=/(\d{2}-\d{2})\s*(\d{2}:\d{2})/.exec(d7s);
        // 续费/重置时间：「下次自动续费时间：2026-08-19」或「2026-08-19 后重置」
        var ex=/(\d{4}-\d{2}-\d{2})/.exec(t);
        if(used==null&&h5==null&&d7==null) return null;
        function iso(m){ if(!m) return null; var y=new Date().getFullYear(); return y+'-'+m[1]+'T'+m[2]+':00'; }
        return {
          subscriptionBalance:{
            amountUsedRatio:used!=null?used/100:null,
            kimiCodeUsedRatio:code!=null?code/100:null,
            expireTime:ex?ex[1]+'T00:00:00Z':null
          },
          ratelimitCode5h:{ratio:h5!=null?h5/100:null,resetTime:iso(r5)},
          ratelimitCode7d:{ratio:d7!=null?d7/100:null,resetTime:iso(r7)}
        };
      }
    })()""" % (SUBSTATS_URL,)
    state = {"ok": False, "fails": 0, "lastOk": 0.0}
    closed = {"by_user": False}  # 用户主动关窗（区别于"清除登录"的静默退出）
    window = webview.create_window(
        "Kimi Board · Kimi 登录（月额度，后台实时刷新）",
        "https://www.kimi.com/membership/subscription?tab=quota",
        width=920, height=760, min_size=(760, 600),
    )

    def _on_closed():
        # 用户点了窗口 X：立即停止后台轮询，并视为"结束会话"
        closed["by_user"] = True

    try:
        window.events.closed += _on_closed
    except Exception:
        pass

    def _loop(window):
        got = False
        # 轮询退出条件：全局停止（清除登录）或 用户关窗
        while not _webview_stop and not closed["by_user"]:
            _webview_dbg["loopAt"] = int(_t.time())
            # 主路径：Python 从 WebView 读 kimi-auth cookie（含 HttpOnly），内存内直接请求 API
            norm = _fetch_via_cookie(window)
            if norm is not None:
                _store_subscription(norm, "webview")
                state["fails"] = 0
                state["lastOk"] = _t.time()
                got = True
                if not state["ok"]:
                    state["ok"] = True
                    _mark_session(True)
                    try:
                        window.hide()
                    except Exception:
                        pass
            else:
                # 兜底：页面内 DOM 抓取 / selfFetch（拿不到拆分字段但数值可用）
                try:
                    res = window.evaluate_js(probe)
                    if isinstance(res, str):
                        try:
                            obj = json.loads(res)
                        except Exception:
                            obj = None
                        if obj and obj.get("ok") and obj.get("data"):
                            handle_subscription_post(obj["data"], "webview")
                            state["fails"] = 0
                            state["lastOk"] = _t.time()
                            got = True
                            if not state["ok"]:
                                state["ok"] = True
                                _mark_session(True)
                                try:
                                    window.hide()
                                except Exception:
                                    pass
                except Exception as e:
                    _webview_dbg["probeErr"] = str(e)[:160]
                # 收集 WebView 内诊断信息（排查用）
                # evaluate_js 会把 JS 对象自动转成 Python dict（不是 JSON 字符串），两种都兼容
                try:
                    dbg = window.evaluate_js("window.__kb_dbg")
                    if isinstance(dbg, dict):
                        _webview_dbg.update(dbg)
                    elif isinstance(dbg, str):
                        try:
                            _webview_dbg.update(json.loads(dbg))
                        except Exception:
                            pass
                except Exception:
                    pass
            if state["ok"]:
                _t.sleep(30 if got else 3)
                got = False
                if _webview_stop or closed["by_user"]:
                    break
                # 健康检查：超过 2.5 分钟没拿到新数据说明会话失效 → 弹窗重新登录
                if _t.time() - state["lastOk"] > 150:
                    state["fails"] += 1
                else:
                    state["fails"] = 0
                if state["fails"] >= 3:
                    state["ok"] = False
                    state["fails"] = 0
                    try:
                        window.show()
                    except Exception:
                        pass
            else:
                _t.sleep(3)
        # 退出分两种：
        #  · 用户关窗（closed["by_user"]）：只是不看，保留登录态，下次可自动重连 → 只 destroy
        #  · 清除登录（_webview_stop）：真正登出 → 清 kimi.com 登录态
        # 窗口可能已被用户关闭 → evaluate_js/destroy 会抛异常，逐个吞掉即可
        if _webview_stop and not closed["by_user"]:
            try:
                window.evaluate_js(
                    "try{localStorage.clear();sessionStorage.clear();"
                    "document.cookie.split(';').forEach(function(c){var n=c.split('=')[0].trim();"
                    "if(n){document.cookie=n+'=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;domain=.kimi.com';"
                    "document.cookie=n+'=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/';}});}catch(e){}")
            except Exception:
                pass
            try:
                webview.delete_cookie("www.kimi.com")
                webview.delete_cookie(".kimi.com")
            except Exception:
                pass
        try:
            window.destroy()
        except Exception:
            pass

    webview.start(_loop, window, private_mode=False,
                  storage_path=str(WEBVIEW_PROFILE_DIR))
    # webview.start 返回有两种原因：_loop 里 destroy（清除登录），或用户点 X 关窗。
    # 关窗只是"暂时不看"：停掉本窗口的轮询循环，但保留登录态与会话标记，
    # 下次启动仍可后台自动重连；只有"清除登录"才真正登出（_mark_session(False)）。
    closed["by_user"] = True  # 兜底：确保 _loop 若还没退出也会随旧 dict 停
    _webview_active = False
    # 从未登录成功就关窗（state["ok"]==False）才会标记会话失效
    if not state["ok"]:
        _mark_session(False)


def webview_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("webview") is not None


def clear_kimi_login() -> dict:
    """清除 Kimi 登录数据：看板缓存 + WebView 持久 profile。"""
    global _webview_stop
    _websub.update(data=None)
    cache = load_cache()
    cache.pop("subscription", None)
    cache.pop("webviewSession", None)
    save_cache(cache)
    if _webview_active:
        # 后台 WebView 在跑：让它自己清 cookie/storage 并退出（占用中的目录不能直接删）
        _webview_stop = True
        return {"ok": True, "message": "已清除缓存，正在清理 WebView 登录态…"}
    # 无 WebView 在跑：直接删持久 profile 目录即可，无需再开窗口
    try:
        import shutil
        shutil.rmtree(str(WEBVIEW_PROFILE_DIR), ignore_errors=True)
    except Exception:
        pass
    return {"ok": True, "message": "已清除看板缓存与 WebView 登录态"}


def webview_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("webview") is not None


def _open_connect() -> dict:
    """入队"打开登录窗口"命令，由主线程消费。"""
    if not webview_available():
        return {"ok": False, "message": "未安装 pywebview：请执行 pip install pywebview，或用浏览器扩展 / 手动 Token 方式"}
    if _webview_active:
        return {"ok": True, "message": "WebView 会话已在后台实时刷新中"}
    try:
        _connect_queue.put_nowait("connect")
        return {"ok": True, "message": "已打开登录窗口：登录后自动隐藏并每 30 秒后台刷新"}
    except Exception:
        return {"ok": False, "message": "登录窗口队列异常"}


def _apply_est(row: dict, est: dict) -> dict:
    """把 _estimate_eta 的结果合并进限额行，供 tooltip 展示估算明细。"""
    row["etaSeconds"] = est.get("etaSeconds")
    row["willHit"] = est.get("willHit")
    row["resetIn"] = est.get("resetIn")
    for k in ("windowSec", "usedPct", "elapsedSec", "remainingSec", "ratePctPerSec",
              "rateSource", "recentLabel"):
        row[k] = est.get(k)
    return row


def _limit_row(name, used_pct, reset_time=None, eta=None, detail=None):
    """统一限额行：全部按百分比展示（limit=100）。used_pct 为 0-100 的已用百分比。"""
    return {
        "name": name, "kind": "percent", "limit": 100,
        "used": round(float(used_pct), 2) if used_pct is not None else None,
        "pct": round(min(float(used_pct), 100.0), 2) if used_pct is not None else 0.0,
        "resetTime": reset_time, "etaSeconds": eta, "detail": detail,
    }


def _recent_rate(used_pct: float, window_total: float, ctx: dict):
    """从 _rate_ctx 取近期速率（已用百分比/秒）。

    优先 15m，样本不足(近期 token 太少)依次退 30m → 60m → 返回 None。
    recent_tokens 折算成"占该窗口已用的比例"× 当前已用百分比，除以窗口秒数。
    返回 (rate_pct_per_sec, label) 或 (None, None)。
    """
    if not used_pct or not window_total or window_total <= 0:
        return None, None
    for label, sec, key in (("15m", 15 * 60, "recent15"),
                            ("30m", 30 * 60, "recent30"),
                            ("60m", 60 * 60, "recent60")):
        rtk = ctx.get(key) or 0
        if rtk <= 0:
            continue
        # 样本充分性：近期 token 至少占窗口已用 token 的 0.5%，否则视为噪声
        if rtk / window_total < 0.005:
            continue
        rate = (rtk / window_total) * used_pct / sec
        return rate, label
    return None, None


def _estimate_eta(used_pct: float, reset_time, window_sec, now: datetime,
                  recent_pct_per_sec=None, recent_label=None):
    """估算触顶情况，优先采用近期速率，样本不足时退回窗口平均速率。

    recent_pct_per_sec: 近期(15/30/60min)折算的"已用百分比/秒"速率；None 表示样本不足。
    recent_label:       近期速率来源（"15m"/"30m"/"60m"），供 tooltip 展示。

    返回 dict：
      etaSeconds: 预计触顶剩余秒数(仅当窗口内可触顶时非 None)
      resetIn:    距本窗口重置的秒数
      willHit:    本窗口内是否会触顶（预计触顶时间 <= 窗口剩余时间）
      windowSec / usedPct / elapsedSec / remainingSec / ratePctPerSec:
        供 tooltip 展示具体估算数值与公式。
      rateSource: "recent"/"window"，本次预测采用的速率来源。
    窗口期内到不了 100% 就明确标记 willHit=False——因为窗口会先重置，
    显示"预计 X 后触顶"反而误导。
    """
    empty = {"etaSeconds": None, "resetIn": None, "willHit": False,
             "windowSec": window_sec, "usedPct": used_pct, "elapsedSec": None,
             "remainingSec": None, "ratePctPerSec": None,
             "rateSource": "window", "recentLabel": None}
    if reset_time is None or not window_sec or used_pct is None:
        return empty
    try:
        dt = reset_time
        if isinstance(dt, str):
            dt = dt.replace("Z", "+00:00")
            dt = datetime.fromisoformat(dt)
        if dt.tzinfo is None:
            # 无时区标记的 resetTime（5h/周限额来自官网）是本地时间，按本地时区处理
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except Exception:
        return empty
    reset_in = (dt - now).total_seconds()
    if reset_in <= 0:
        return empty  # 窗口已结束/过期
    elapsed = max(1.0, window_sec - reset_in)
    used = max(0.0, float(used_pct))
    win_rate = used / elapsed  # 窗口平均：已用百分比 / 已流逝秒数

    # 选定预测速率：优先近期速率（样本充分时），否则退回窗口平均
    rate = recent_pct_per_sec if recent_pct_per_sec and recent_pct_per_sec > 0 else None
    rate_source = "recent" if rate is not None else "window"
    if rate is None:
        rate = win_rate

    empty.update(elapsedSec=round(elapsed), remainingSec=int(reset_in),
                 ratePctPerSec=round(rate, 6), rateSource=rate_source,
                 recentLabel=recent_label if rate_source == "recent" else None)
    if rate <= 0:
        return empty
    eta = int((100.0 - used) / rate)
    will_hit = eta < reset_in  # 触顶必须发生在窗口重置之前才算
    if not will_hit:
        eta = None
    return {"etaSeconds": eta, "resetIn": int(reset_in), "willHit": will_hit,
            "windowSec": window_sec, "usedPct": used_pct, "elapsedSec": round(elapsed),
            "remainingSec": int(reset_in), "ratePctPerSec": round(rate, 6),
            "rateSource": rate_source, "recentLabel": recent_label if rate_source == "recent" else None}


def integrated_limits(cfg: dict) -> dict:
    """整合限额展示，数据源回退链：
    1) 官网 GetSubscriptionStats（登录后，百分比精确到两位小数：月额度 / 5h / 周 + 官方提示）
    2) 无官网登录 → KimiCode 同步（本地 kimi web → 云端 API，整数百分比：周 / 5h）
    """
    official = subscription_snapshot(cfg)
    if official.get("ok") and official.get("data"):
        d = official["data"]
        rows = []
        now = datetime.now().astimezone()
        if d.get("amountUsedRatio") is not None:
            total = d["amountUsedRatio"] * 100
            # 月额度窗口 = 当前计费周期长度（天）；起算到续费时间做窗口结束推算
            cycle_days = cycle_bounds(datetime.now(), cfg).get("daysInCycle") or 30
            rrate, rlabel = _recent_rate(total, _rate_ctx.get("monthTotal"), _rate_ctx)
            est = _estimate_eta(total, d.get("expireTime"), cycle_days * 86400, now,
                                recent_pct_per_sec=rrate, recent_label=rlabel)
            row = _apply_est(_limit_row("月额度（官网订阅）", total, d.get("expireTime"), est["etaSeconds"]), est)
            # 月额度 = Kimi 用量 + KimiCode 用量（kimiCodeUsedRatio 是占总月额度的比例）
            if d.get("kimiCodeUsedRatio") is not None:
                code_pct = d["kimiCodeUsedRatio"] * 100
                row["kimiCodePct"] = round(min(code_pct, 100.0), 2)
                row["kimiPct"] = round(max(0.0, min(total, 100.0) - code_pct), 2)
            rows.append(row)
        l5 = d.get("limits5h") or {}
        if l5.get("ratio") is not None:
            rrate, rlabel = _recent_rate(l5["ratio"] * 100, _rate_ctx.get("h5Total"), _rate_ctx)
            est = _estimate_eta(l5["ratio"] * 100, l5.get("resetTime"), 5 * 3600, now,
                                recent_pct_per_sec=rrate, recent_label=rlabel)
            rows.append(_apply_est(_limit_row("5 小时限额", l5["ratio"] * 100, l5.get("resetTime"), est["etaSeconds"]), est))
        l7 = d.get("limits7d") or {}
        if l7.get("ratio") is not None:
            rrate, rlabel = _recent_rate(l7["ratio"] * 100, _rate_ctx.get("d7Total"), _rate_ctx)
            est = _estimate_eta(l7["ratio"] * 100, l7.get("resetTime"), 7 * 86400, now,
                                recent_pct_per_sec=rrate, recent_label=rlabel)
            rows.append(_apply_est(_limit_row("周限额", l7["ratio"] * 100, l7.get("resetTime"), est["etaSeconds"]), est))
        notice = None
        if d.get("notice"):
            notice = d["notice"].get("tip") or d["notice"].get("content")
        return {"ok": True, "source": "official", "rows": rows, "notice": notice,
                "fetchedAt": official.get("fetchedAt")}
    # 回退：KimiCode 同步（本地 kimi web → 云端），整数百分比
    quota = quota_snapshot(cfg)
    if quota.get("ok"):
        rows = [_limit_row(r.get("name", "限额"), r.get("pct", 0), r.get("resetAt"),
                           r.get("etaSeconds")) for r in quota.get("rows", [])]
        return {"ok": True, "source": quota.get("source", "kimi"), "rows": rows,
                "notice": None, "fetchedAt": quota.get("fetchedAt")}
    return {"ok": False, "source": None, "rows": [], "notice": None,
            "message": quota.get("message") or official.get("message") or "暂无限额数据"}


def cycle_bounds(now: datetime, cfg: dict) -> dict:
    """当前计费周期与上一周期边界。cfg.billing: {day, hour, minute}。"""
    bill = cfg["billing"]
    day = max(1, min(int(bill.get("day", 1)), 31))
    hour = max(0, min(int(bill.get("hour", 0)), 23))
    minute = max(0, min(int(bill.get("minute", 0)), 59))

    def anchor(dt):
        dim = calendar.monthrange(dt.year, dt.month)[1]
        return dt.replace(day=min(day, dim), hour=hour, minute=minute,
                          second=0, microsecond=0)

    def shift(dt, delta_months):
        m = dt.month - 1 + delta_months
        return datetime(dt.year + m // 12, m % 12 + 1, 1,
                        hour=dt.hour, minute=dt.minute, second=dt.second)

    start = anchor(now)
    if start > now:
        start = anchor(shift(now, -1))
    end = anchor(shift(start, 1))
    if end <= start:  # 防御：如 day 钳制导致重叠则延后到下月
        end = anchor(shift(start, 2))
    prev_start = anchor(shift(start, -1))
    return {
        "start": start, "end": end, "prevStart": prev_start,
        "daysInCycle": (end - start).days,
        "daysElapsed": max(1, (now - start).days),
        "label": start.strftime("%m-%d %H:%M"),
    }


VERSION = "2.0.0"  # 与最新 release 标签（去 v 前缀）保持一致，发版时更新
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
    cfg = CFG
    home = kimi_home()
    sessions_dir = home / "sessions"
    now = datetime.now()
    now_ms = int(now.timestamp() * 1000)

    # ---- 计费周期（默认每月 1 日 00:00，可在设置页配置到分钟） ----
    cb = cycle_bounds(now, cfg)
    month_start = int(cb["start"].timestamp() * 1000)
    month_end = int(cb["end"].timestamp() * 1000)
    prev_start, prev_end = int(cb["prevStart"].timestamp() * 1000), month_start
    days_in_cycle = cb["daysInCycle"]
    days_elapsed = cb["daysElapsed"]
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
    # 近期速率统计：最近 15/30/60 分钟的用量（供触顶预测优先采用）
    recent_win = {"15m": empty_usage(), "30m": empty_usage(), "60m": empty_usage()}
    recent_ms = {"15m": 15 * 60 * 1000, "30m": 30 * 60 * 1000, "60m": 60 * 60 * 1000}
    # 本账期按"周期日"分桶：每个桶 = 从起算时刻起每 24h 一段（而不是按自然日 0 点切）。
    # 桶 0 = month_start → month_start+24h，桶 1 = +24h→+48h ……
    # 这样起算日当天不再被 0 点边界切成两半，每根柱子都精确等于一个周期日的用量，
    # 总和与首页"本账期"(cards.month) 严格一致。
    cycle_day_ms = 24 * 3600 * 1000
    cycle_daily = []  # 动态扩展，索引即周期日序号
    cycle_daily_models = []
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

                if month_start <= t < month_end:
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
                if month_start <= t < month_end:
                    i = (t - month_start) // cycle_day_ms
                    while i >= len(cycle_daily):
                        cycle_daily.append(empty_usage())
                        cycle_daily_models.append(defaultdict(empty_usage))
                    add(cycle_daily[i])
                    add(cycle_daily_models[i][model])
                for _rk in ("15m", "30m", "60m"):
                    if t >= now_ms - recent_ms[_rk]:
                        add(recent_win[_rk])

    def finalize(u):
        inp = u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"]
        return {
            "input": inp,
            "cacheRead": u["inputCacheRead"],
            "output": u["output"],
            "total": inp + u["output"],
        }

    # ---- 费用估算（价格表由设置页/CLI 决定，可自动同步） ----
    refresh_pricing()
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
    # 合并上一周期有消耗但本周期未用的模型，保证每个模型都能看到费用
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

    # ---- 近期速率上下文（供触顶预测优先采用近期速率） ----
    try:
        def _tok_total(u):
            return u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"] + u["output"]
        _rate_ctx.update(
            recent15=_tok_total(recent_win["15m"]),
            recent30=_tok_total(recent_win["30m"]),
            recent60=_tok_total(recent_win["60m"]),
            monthTotal=_tok_total(cards["month"]),
            h5Total=sum(_tok_total(h) for h in hourly[-5:]),
            d7Total=sum(_tok_total(d_) for d_ in daily[-7:]),
        )
    except Exception:
        pass

    pace = month_cost / days_elapsed * days_in_cycle if days_elapsed else 0.0

    # 套餐价：配置显式价 > 配置档位 > 自动识别（kimi web 在线时）> 默认 199
    plan_price, plan_name, plan_is_auto, plan_src = resolve_plan(cfg)
    payback_pct = round(month_cost / plan_price * 100, 1) if plan_price > 0 else None

    # ---- 官方配额（5 小时 / 周限额），失败不阻塞看板 ----
    try:
        quota = quota_snapshot(cfg)
    except Exception:
        quota = {"ok": False, "enabled": True, "message": "配额同步异常", "rows": [], "fetchedAt": 0}

    # ---- 月额度（官网 GetSubscriptionStats） ----
    try:
        subscription = subscription_snapshot(cfg)
    except Exception:
        subscription = {"ok": False, "enabled": True, "message": "月额度同步异常", "data": None}

    # ---- 整合限额：官网(百分比) → KimiCode(百分比，本地→云端) ----
    try:
        limits = integrated_limits(cfg)
    except Exception:
        limits = {"ok": False, "source": None, "rows": [], "notice": None,
                  "message": "限额整合异常"}

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
            "prevMonthLabel": cb["prevStart"].strftime("%Y-%m"),
            "prevMonthTotal": round(prev_cost, 2),
            "planPrice": plan_price,
            "planName": plan_name,
            "planAuto": plan_is_auto,
            "planSource": plan_src,
            "paybackPct": payback_pct,
            "pace": round(pace, 2),
            "daysElapsed": days_elapsed,
            "daysInCycle": days_in_cycle,
            "cycleLabel": cb["label"],
            "cycleStart": int(cb["start"].timestamp() * 1000),
            "cycleEnd": int(cb["end"].timestamp() * 1000),
        },
        "pricing": pricing_info(),
        "quota": quota,
        "subscription": subscription,
        "limits": limits,
        "billing": {
            "day": cfg["billing"]["day"],
            "hour": cfg["billing"]["hour"],
            "minute": cfg["billing"]["minute"],
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
        "cycleDaily": [
            {"t": month_start + i * cycle_day_ms, **finalize(u),
             "models": {m: v for m, mu in cycle_daily_models[i].items()
                        if (v := finalize(mu)["total"]) > 0}}
            for i, u in enumerate(cycle_daily)
        ],
    }


# ---------------------------------------------------------------- 页面

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/favicon.png">
<title>Kimi Code 用量看板</title>
<meta name="kb-secret" content="__KB_SECRET__">
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
  #settingsLink {
    font-size: 13px; font-weight: 550; color: var(--blue-deep);
    text-decoration: none; border: 1px solid #c9dfff; background: #f2f8ff;
    border-radius: 10px; padding: 7px 14px;
    transition: background .15s ease, transform .1s ease;
  }
  #settingsLink:hover { background: #e2efff; }
  #settingsLink:active { transform: scale(.96); }
  .update-tip {
    color: var(--blue-deep); text-decoration: none; margin-left: 10px;
    letter-spacing: inherit;
  }
  .update-tip:hover { text-decoration: underline; }

  /* ---- 官方限额 ---- */
  .quota-rows { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
  .quota-row {
    background: #f7faff; border: 1px solid #e3ecfb; border-radius: 14px;
    padding: 16px 18px 14px;
  }
  .quota-row .q-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
  .quota-row .q-name { font-weight: 650; font-size: 14px; color: var(--text); }
  .quota-row .q-window { font-size: 11px; color: var(--faint); font-family: var(--mono); }
  .quota-row .q-nums {
    display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px;
    font-variant-numeric: tabular-nums;
  }
  .quota-row .q-used { font-family: var(--num); font-size: 26px; font-weight: 700; color: var(--text); }
  .quota-row .q-of { font-size: 12px; color: var(--dim); font-family: var(--mono); }
  .quota-row .q-pct { margin-left: auto; font-size: 14px; font-weight: 650; color: var(--blue-deep); font-family: var(--num); }
  .quota-row .q-pct.warn { color: #e8833a; }
  .quota-row .q-pct.danger { color: #e5484d; }
  .quota-row .q-track { height: 8px; background: #e6edf8; border-radius: 999px; overflow: hidden; margin-bottom: 10px; }
  .quota-row .q-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #5ea2ff, #2e6fe8); }
  .quota-row .q-fill.warn { background: linear-gradient(90deg, #ffb454, #ff7a3d); }
  .quota-row .q-fill.danger { background: linear-gradient(90deg, #ff7a3d, #e5484d); }
  /* 拆分段：两段同色系，Kimi 浅 / KimiCode 深；末段右端圆角让填充末端自然收圆 */
  .quota-row .q-track.split { display: flex; overflow: hidden; }
  .quota-row .q-track.split > div { height: 100%; }
  .quota-row .q-track.split > div:first-child { border-radius: 999px 0 0 999px; }
  .quota-row .q-track.split > div:last-child { border-radius: 0 999px 999px 0; }
  .quota-row .q-track.split .kimi { background: #b9d6fc; }
  .quota-row .q-track.split .code { background: linear-gradient(90deg, #5ea2ff, #2e6fe8); }
  .quota-row .q-track.split.warn .kimi { background: #ffd9ad; }
  .quota-row .q-track.split.warn .code { background: linear-gradient(90deg, #ffb454, #ff7a3d); }
  .quota-row .q-track.split.danger .kimi { background: #f5bcbc; }
  .quota-row .q-track.split.danger .code { background: linear-gradient(90deg, #ff7a3d, #e5484d); }
  .quota-row .q-foot { display: flex; justify-content: space-between; gap: 10px; font-size: 11.5px; color: var(--dim); }
  .quota-row .q-foot .num { font-family: var(--mono); color: var(--text); }
  .quota-empty { font-size: 12.5px; color: var(--faint); line-height: 1.9; }

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
  .card .value .value-unit { font-size: 15px; font-weight: 600; color: var(--faint); margin-left: 8px; letter-spacing: .02em; }
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
  /* hover 时抬高整个费用网格，避免 tooltip 被后面的 model-cost stacking context 盖住 */
  .blackcard .cost-grid:hover { z-index: 10; }
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
    position: absolute; left: -9px; top: 22px; z-index: 100;
    width: min(420px, calc(100vw - 32px)); max-width: calc(100vw - 32px);
    background: #1b2233; border: 1px solid #31405e; border-radius: 10px;
    padding: 11px 14px 10px; font-size: 11.5px; font-weight: 400; line-height: 1.6; color: #8f9ab0;
    opacity: 0; visibility: hidden; transform: translateY(-4px);
    transition: opacity .15s ease, transform .15s ease, visibility .15s;
    pointer-events: none;
  }
  .blackcard .help:hover .tip { opacity: 1; visibility: visible; transform: translateY(0); }
  .blackcard .help .tp-f { display: block; margin-bottom: 8px; white-space: nowrap; }
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
  .blackcard .help .tp-note { display: block; border-top: 1px solid #2a3450; padding-top: 6px; font-size: 10.5px; color: #5d6a85; word-break: keep-all; }
  .blackcard .big { font-family: var(--num); font-size: 50px; font-weight: 700; letter-spacing: .01em; font-variant-numeric: tabular-nums; margin-bottom: 14px; }

  /* 官方限额旁的帮助图标（浅色主题版） */
  #quotaSec { position: relative; z-index: 2; }
  #quotaSec:hover { z-index: 30; }
  .q-help.help {
    position: relative;
    display: inline-flex; align-items: center; justify-content: center;
    color: #a8b4cc; cursor: help;
    transition: color .15s ease;
  }
  .q-help.help svg { width: 13px; height: 13px; display: block; }
  .q-help.help:hover { color: var(--blue-deep); }
  .q-help.help .tip {
    position: absolute; left: -9px; top: 22px; z-index: 100;
    width: min(420px, calc(100vw - 32px)); max-width: calc(100vw - 32px);
    background: #fff; border: 1px solid #d3e3fb; border-radius: 10px;
    box-shadow: 0 6px 22px rgba(30,60,120,.14);
    padding: 11px 14px; font-size: 11.5px; font-weight: 400; line-height: 1.7; color: #5d6b82;
    opacity: 0; visibility: hidden; transform: translateY(-4px);
    transition: opacity .15s ease, transform .15s ease, visibility .15s;
    pointer-events: none;
  }
  .q-help.help:hover .tip { opacity: 1; visibility: visible; transform: translateY(0); }
  .q-help.help .tp-f { display: block; }
  .q-help.help .tp-f b { color: #101828; font-weight: 600; }

  /* 计算明细：点开才展开的紧凑表格 */
  .quota-toggle {
    align-self: center;
    display: inline-flex; align-items: center; justify-content: center; gap: 5px;
    font-size: 11.5px; line-height: 1.6; font-weight: 600; color: var(--blue-deep);
    background: rgba(58,141,255,.10); border: 1px solid #cfe3ff; border-radius: 999px;
    padding: 1px 12px; cursor: pointer; margin-left: 2px;
    transition: background .15s ease;
  }
  .quota-toggle:hover { background: rgba(58,141,255,.18); }
  .quota-toggle .tgl-label { display: inline-flex; align-items: center; transform: translateY(0.5px); }
  .quota-toggle .chev {
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 9px; transition: transform .18s ease;
  }
  .quota-toggle[aria-expanded="true"] .chev { transform: rotate(180deg); }
  .quota-detail { margin-top: 14px; }
  .quota-detail table {
    width: 100%; border-collapse: collapse;
    font-size: 12px; color: var(--dim);
  }
  .quota-detail th, .quota-detail td {
    padding: 6px 10px; text-align: left;
    border-bottom: 1px solid #edf1f8;
    font-variant-numeric: tabular-nums;
  }
  .quota-detail th {
    font-size: 11px; font-weight: 600; color: var(--faint); letter-spacing: .05em;
  }
  .quota-detail td:first-child { font-weight: 600; color: var(--text); }
  .quota-detail td .num { font-family: var(--mono); color: var(--text); }
  .quota-detail td .dim { color: var(--faint); font-size: 11px; }
  .quota-detail .d-note { margin-top: 8px; font-size: 10.5px; color: var(--faint); }
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
  /* 官方限额区块的标题/图标/按钮统一垂直居中，避免 baseline 造成按钮下坠 */
  #quotaSec .sec-head { align-items: center; line-height: 1.6; }

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
  <a id="settingsLink" href="/settings" title="设置">设置</a>
  <button id="refresh">刷新</button>
</div>

<div class="hero reveal">
  <div class="glyph-strip" aria-hidden="true">− + / ( ) * ▲ K # ⬡ &nbsp; − + / ( ) * ▲ K # ⬡</div>
  <div class="meta" id="heroMeta"></div>
</div>

<div class="cards reveal" id="cards" style="animation-delay:40ms"></div>

<section class="quota reveal" id="quotaSec" style="animation-delay:60ms">
  <div class="sec-head">官方限额
    <span class="q-help help" title="触顶时间怎么算"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.2"/><path d="M9.4 9a2.7 2.7 0 0 1 5.25.9c0 1.8-2.65 2.4-2.65 3.6"/><line x1="12" y1="16.8" x2="12.01" y2="16.8"/></svg><span class="tip">
      <span class="tp-f">触顶时间按<b>近期 15 / 30 / 60 分钟</b>速率估算；样本不足时采用<b>窗口平均</b>；预测仅供参考，用量突变时可能偏差较大。</span>
    </span></span>
    <button class="quota-toggle" id="quotaToggle" aria-expanded="false"><span class="tgl-label">计算明细</span> <span class="chev">▾</span></button>
    <span class="right" id="quotaMeta"></span>
  </div>
  <div class="quota-rows" id="quotaRows"></div>
  <div class="quota-detail" id="quotaDetail" hidden></div>
  <div class="caption">QUOTA · 官网百分比(两位) → KimiCode(整数) 自动回退 · 每次刷新自动更新</div>
</section>

<div class="blackcard reveal" style="animation-delay:80ms">
  <div class="cost-grid">
    <div class="cost-left">
      <div class="glyph-field" aria-hidden="true"><div class="glyph-inner" id="glyphField"></div></div>
      <div class="label"><span class="hex" style="font-size:12px">⬡</span> 等效 API 费用 · <span id="costLabel">本月</span>
        <span class="help"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.2"/><path d="M9.4 9a2.7 2.7 0 0 1 5.25.9c0 1.8-2.65 2.4-2.65 3.6"/><line x1="12" y1="16.8" x2="12.01" y2="16.8"/></svg><span class="tip">
          <span class="tp-f"><b>等效费用</b> = 缓存×缓存价 + 输入×输入价 + 输出×输出价</span>
          <span class="tp-t" id="priceRows">
            <span class="tp-r tp-h"><b>元 / 1M</b><i>缓存</i><i>输入</i><i>输出</i></span>
          </span>
          <span class="tp-note" id="priceNote">刊例价估算 · 非实际账单 · 缓存创建按输入价计</span>
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
    <span class="right seg" id="rangeSeg"><span class="seg-pill" aria-hidden="true"></span><button data-r="24h">24 小时</button><button data-r="7d" class="on">7 天</button><button data-r="30d">30 天</button><button data-r="mtd">本月</button><button data-r="cycle">本账期</button></span>
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
// 中文缩略单位（仅亿/万；<1万 返回空，不用显示）：
// 627686630 -> "≈6.28亿"，62768 -> "≈6.3万"，9999 -> ""
const fmtCN = n => n >= 1e8 ? "≈" + (n / 1e8).toFixed(2).replace(/\.?0+$/, "") + "亿"
             : n >= 1e4 ? "≈" + (n / 1e4).toFixed(1).replace(/\.0$/, "") + "万"
             : "";
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
    <div class="value" data-v="${c.total}"><span class="value-num">0</span>${fmtCN(c.total) ? `<span class="value-unit">${fmtCN(c.total)}</span>` : ""}</div>
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
  // 本账期：后端 cycleDaily 已按精确周期边界 [cycleStart, cycleEnd) 只计入周期内事件，
  // 起算日当天(部分时段)也正确归入当天的桶。这里不要再按 t>=cycleStart 过滤——
  // 起算日 00:00 的桶时间戳早于 cycleStart，但桶内全是本周期数据，过滤会整桶丢。
  // 只需保留有数据的桶即可，总和与首页"本账期"卡片严格一致。
  if (TREND.range === "cycle") {
    const pts = (d.cycleDaily || []).filter(p => p.total > 0);
    return { pts, kind: "day" };
  }
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
    <div class="ts"><span class="lb">合计</span><span class="vl"><b id="tsTotal">0</b><span class="unit">Tokens</span></span></div>
    <div class="ts"><span class="lb">峰值</span><span class="vl"><b>${fmtK(peak.total)}</b><span class="unit">${esc(kind === "hour" ? trendLabel(peak, kind, true).slice(5) : trendLabel(peak, kind, true))}</span></span></div>
    <div class="ts"><span class="lb">${kind === "hour" ? "时均" : "日均"}</span><span class="vl"><b>${fmtK(avg)}</b><span class="unit">Tokens</span></span></div>`;
  // 合计沿用原来的紧凑显示（M/K）
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

const SRC_TXT = {custom: "手动价", tier: "手动档位", auto: "自动识别", default: "默认价"};
function paybackHtml(c) {
  const srcTxt = SRC_TXT[c.planSource] || "";
  const planLabel = c.planName
    ? `${c.planName} · ¥${c.planPrice} 套餐${srcTxt ? " · " + srcTxt : ""}`
    : `¥${c.planPrice} 套餐（${srcTxt || "默认"}）`;
  if (c.planPrice <= 0 || c.paybackPct === null) {
    return `<div class="pb-label">${c.planName || "免费"} 套餐 · 无月费</div>
      <div class="pb-value">¥0</div>
      <div class="pb-note">本账期等效用量价值 ${yuan(c.monthTotal)}</div>
      <div class="pb-note">上一账期（${c.prevMonthLabel}）等效 ${yuan(c.prevMonthTotal)}</div>`;
  }
  const pct = c.paybackPct;
  const w = Math.min(pct, 100).toFixed(1);
  const verdict = pct >= 100
    ? `已回本 ${(pct / 100).toFixed(1)} 倍`
    : `还差 ${yuan(c.planPrice - c.monthTotal)} 回本`;
  const pace = `当前节奏 · 账期预估 ${yuan(c.pace)}`;
  return `<div class="pb-label">${planLabel} · 本账期回本率</div>
    <div class="pb-value">${pct}%</div>
    <div class="pb-bar"><div class="pb-fill" style="width:${w}%"></div></div>
    <div class="pb-note">${verdict} · ${pace}</div>
    <div class="pb-note">上一账期（${c.prevMonthLabel}）等效 ${yuan(c.prevMonthTotal)}</div>`;
}

const fmtNum = n => (n % 1 === 0 ? n : +n.toFixed(2));function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.round((s % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分`;
}

function renderPricing(p) {
  const rows = Object.entries(p.table || {}).map(([m, v]) =>
    `<span class="tp-r"><b>${esc(shortName(m))}</b><i>${fmtNum(v[0])}</i><i>${fmtNum(v[1])}</i><i>${fmtNum(v[2])}</i></span>`
  ).join("");
  document.getElementById("priceRows").innerHTML =
    `<span class="tp-r tp-h"><b>${p.currency === "USD" ? "$" : "元"} / 1M</b><i>缓存</i><i>输入</i><i>输出</i></span>` + rows;
  document.getElementById("priceNote").textContent =
    `${esc(p.message || "")}${p.fetchedAt ? " · " + new Date(p.fetchedAt).toLocaleTimeString("zh-CN", {hour12: false}) : ""} · 缓存创建按输入价计`;
}

function pctStr(v, dp) {
  if (v == null) return "--";
  const n = dp ? +(+v).toFixed(dp) : Math.round(v * 10) / 10;
  return (n % 1 === 0 ? n : n.toFixed(2).replace(/\.?0+$/, "")) + "%";
}
function renderLimits(l) {
  const meta = document.getElementById("quotaMeta");
  const el = document.getElementById("quotaRows");
  if (!l || !l.ok) {
    meta.textContent = "";
    el.innerHTML = `<div class="quota-empty">暂无限额数据：${esc((l && l.message) || "")}<br>
      可在 <a href="/settings">设置</a> 「连接 Kimi」登录官网拿精确百分比，或确认 kimi web / 已登录。</div>`;
    return;
  }
  const srcTxt = {official: "官网 · 百分比(两位)", local: "KimiCode · 本机", cloud: "KimiCode · 云端"}[l.source] || l.source;
  meta.textContent = `${srcTxt} · 同步于 ${new Date(l.fetchedAt).toLocaleTimeString("zh-CN", {hour12: false})}`;
  if (!l.rows.length) {
    el.innerHTML = `<div class="quota-empty">官方未返回限额数据（账号 / 地区可能不适用）。</div>`;
    return;
  }
  el.innerHTML = l.rows.map(r => {
    const pct = Math.min(100, r.pct);
    // 用量 >=80 警示橙、>=95 危险红
    const cls = r.pct >= 95 ? " danger" : r.pct >= 80 ? " warn" : "";
    // 触顶预测：窗口内可触顶才显示"预计 X 后触顶"；否则说明本窗口到不了 100%（会先重置）
    const eta = r.willHit && r.etaSeconds != null
      ? `预计约 ${fmtDur(r.etaSeconds)} 后触顶`
      : (r.willHit === false && r.resetIn != null ? "预计本窗口内不会触顶" : "");
    const reset = r.resetTime ? `重置 ${new Date(r.resetTime).toLocaleString("zh-CN", {hour12: false})}` : "";
    const isSplit = r.kimiCodePct != null;
    const track = isSplit
      ? `<div class="q-track split${cls}"><div class="kimi" style="width:${Math.min(100, r.kimiPct)}%"></div><div class="code" style="width:${Math.min(100, r.kimiCodePct)}%"></div></div>`
      : `<div class="q-track"><div class="q-fill${cls}" style="width:${pct}%"></div></div>`;
    const detail = isSplit
      ? `<div class="q-foot"><span class="num">其中 Kimi ${pctStr(r.kimiPct, 2)} | KimiCode ${pctStr(r.kimiCodePct, 2)}</span></div>`
      : (r.detail ? `<div class="q-foot"><span class="num">${esc(r.detail)}</span></div>` : "");
    return `<div class="quota-row">
      <div class="q-head"><span class="q-name">${esc(r.name)}</span></div>
      <div class="q-nums"><span class="q-used">${pctStr(r.used, 2)}</span><span class="q-of">已用</span><span class="q-pct${cls}">剩余 ${pctStr(Math.max(0, 100 - r.pct), 1)}</span></div>
      ${track}
      <div class="q-foot"><span class="num">${esc(reset || "")}</span><span>${eta}</span></div>
      ${detail}
    </div>`;
  }).join("");
  // 计算明细表：点"计算明细"才展开，紧凑四列
  const detail = document.getElementById("quotaDetail");
  if (detail) {
    const durShort = s => {
      s = Math.max(0, Math.round(s));
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
      return d ? `${d}天${h}时` : `${h}时`;
    };
    const srcTxt = r => r.rateSource === "recent"
      ? `近期${r.recentLabel}` : "窗口平均";
    const rowTxt = r => {
      if (r.usedPct == null || r.windowSec == null) return "";
      const prog = `${durShort(r.elapsedSec)} / ${durShort(r.windowSec)}`;
      const rate = `${fmtNum((r.ratePctPerSec || 0) * 3600)}%/时`;
      const eta = r.willHit && r.etaSeconds != null
        ? `约 ${durShort(r.etaSeconds)}`
        : (r.willHit === false ? "重置前不触顶" : "--");
      return `<tr>
        <td>${esc(r.name)}</td>
        <td><span class="num">${prog}</span></td>
        <td><span class="num">${rate}</span> <span class="dim">· ${srcTxt(r)}</span></td>
        <td><span class="num">${eta}</span></td>
      </tr>`;
    };
    detail.innerHTML = `<table>
      <thead><tr><th>限额</th><th>窗口进度</th><th>采用速率</th><th>预计触顶</th></tr></thead>
      <tbody>${l.rows.map(rowTxt).join("")}</tbody>
    </table><div class="d-note">触顶时间按近期 15/30/60 分钟速率估算；样本不足时采用窗口平均。预测仅供参考，用量突变时可能偏差较大。</div>`;
    // 打开状态下保持展开（30s 自动刷新时状态不丢失）
    const toggle = document.getElementById("quotaToggle");
    if (toggle) {
      const wasOpen = toggle.getAttribute("aria-expanded") === "true";
      detail.hidden = !wasOpen;
    }
  }
  if (l.notice) {
    el.innerHTML += `<div class="quota-empty" style="margin-top:12px">${esc(l.notice)}</div>`;
  }
}

async function load() {
  const updated = document.getElementById("updated");
  try {
    const d = await (await fetch("/api/stats", {cache: "no-store"})).json();
    const c = d.cost;
    document.getElementById("heroMeta").innerHTML =
      `<span class="hex">⬡</span>${esc((c.planName || "FREE PLAN").toUpperCase())} · ${fmt(d.turns)} TURNS TRACKED · 账期 ${esc(c.cycleLabel)}`;
    document.getElementById("footMeta").textContent =
      `数据来自本机 wire 文件 · 更新于 ${new Date(d.generatedAt).toLocaleTimeString("zh-CN", {hour12: false})} · 点刷新同步`;

    document.getElementById("costLabel").textContent = c.cycleLabel;
    document.getElementById("cards").innerHTML =
      cardHtml("本账期", "USAGE · 01 CYCLE", d.cards.month, true) +
      cardHtml("今日", "USAGE · 02 TODAY", d.cards.today, false) +
      cardHtml("近 1 小时", "USAGE · 03 HOUR", d.cards.hour, false);
    document.querySelectorAll("#cards .value").forEach(el =>
      countUp(el.querySelector(".value-num"), +el.dataset.v, v => fmt(Math.round(v))));

    countUp(document.getElementById("costTotal"), c.monthTotal, yuan);
    document.getElementById("costBreakdown").innerHTML = `
      <div class="row"><span>缓存命中</span><span class="num">${yuan(c.components.cache)}</span></div>
      <div class="row"><span>输入（未命中）</span><span class="num">${yuan(c.components.miss)}</span></div>
      <div class="row"><span>输出</span><span class="num">${yuan(c.components.out)}</span></div>`;
    document.getElementById("payback").innerHTML = paybackHtml(c);
    modelCostList(document.getElementById("modelCost"), c.byModel);
    renderPricing(d.pricing);
    renderLimits(d.limits);

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
document.getElementById("quotaToggle").onclick = function () {
  const d = document.getElementById("quotaDetail");
  const open = d.hidden;
  d.hidden = !open;
  this.setAttribute("aria-expanded", open ? "true" : "false");
};
load();

// 限额区每 30 秒自动刷新（页面隐藏时暂停）
async function refreshQuotaLight() {
  if (document.hidden) return;
  try {
    const d = await (await fetch("/api/stats", {cache: "no-store"})).json();
    renderLimits(d.limits);
    const upd = document.getElementById("updated");
    if (upd) upd.textContent = `更新于 ${new Date(d.generatedAt).toLocaleString("zh-CN", {hour12: false})} · ${fmt(d.turns)} turns`;
  } catch (e) { /* 网络瞬时失败静默，等下一轮 */ }
}
setInterval(refreshQuotaLight, 30000);

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

# ---------------------------------------------------------------- 设置页

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/favicon.png">
<title>Kimi Board · 设置</title>
<meta name="kb-secret" content="__KB_SECRET__">
<style>
  :root {
    --bg: #f5f8fd; --card: #ffffff; --line: #e3e9f4; --text: #101828;
    --dim: #5d6b82; --faint: #a8b4cc; --blue: #3a8dff; --blue-deep: #2e6fe8;
    --mono: ui-monospace, "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
    --sans: "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; }
  html { background: var(--bg); }
  body { background: transparent; color: var(--text); font-family: var(--sans); padding: 26px 32px 60px; max-width: 960px; margin: 0 auto; }
  .topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 26px; }
  .hex { color: var(--blue); font-size: 18px; }
  .crumb { font-size: 13px; color: var(--dim); }
  .crumb b { color: var(--text); font-weight: 600; }
  .spacer { flex: 1; }
  a.back { font-size: 13px; font-weight: 550; color: var(--blue-deep); text-decoration: none; border: 1px solid #c9dfff; background: #f2f8ff; border-radius: 10px; padding: 7px 14px; }
  a.back:hover { background: #e2efff; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 20px 24px; margin-bottom: 16px; }
  .sec-head { font-size: 15px; font-weight: 700; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
  .sec-head .hex { font-size: 13px; }
  .sec-desc { font-size: 12px; color: var(--dim); font-weight: 400; margin-left: 4px; }
  .row { display: flex; align-items: center; gap: 10px; margin: 12px 0; flex-wrap: wrap; }
  .row label { font-size: 13px; color: var(--dim); width: 180px; flex: 0 0 auto; }
  .row .hint { font-size: 11.5px; color: var(--faint); margin-left: 4px; }
  select, input[type=number], input[type=text] {
    font-family: var(--mono); font-size: 13px; color: var(--text);
    border: 1px solid #d5e0f0; border-radius: 9px; padding: 7px 10px; background: #fbfdff; outline: none;
  }
  select:focus, input:focus { border-color: var(--blue); }
  input[type=number] { width: 84px; }
  input.wide { width: 140px; }
  .radio { display: inline-flex; gap: 18px; flex-wrap: wrap; }
  .radio label { width: auto; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; color: var(--text); }
  .price-grid { margin: 10px 0 6px; }
  .price-grid .pg-head, .price-grid .pg-row {
    display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr; gap: 10px; align-items: center;
  }
  .price-grid .pg-head { font-size: 11px; color: var(--faint); font-family: var(--mono); letter-spacing: .08em; padding: 0 2px 6px; }
  .price-grid .pg-row { padding: 4px 2px; }
  .price-grid .pg-row .pn { font-size: 13px; color: var(--text); font-family: var(--mono); }
  .price-grid input { width: 100%; }
  .btn { font-size: 13px; font-weight: 550; color: #fff; border: none; cursor: pointer; background: var(--blue); border-radius: 10px; padding: 9px 20px; transition: background .15s ease, transform .1s ease; }
  .btn:hover { background: var(--blue-deep); }
  .btn:active { transform: scale(.97); }
  .btn.ghost { background: #eef4ff; color: var(--blue-deep); border: 1px solid #c9dfff; }
  .btn.ghost:hover { background: #e2efff; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .actions { display: flex; gap: 12px; margin-top: 6px; flex-wrap: wrap; }
  #status { font-size: 12.5px; color: var(--dim); margin-top: 10px; min-height: 18px; line-height: 1.7; }
  #status.ok { color: #128a5b; }
  #status.err { color: #e5484d; }
  .mini-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .mini-table th, .mini-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eef1f6; font-family: var(--mono); }
  .mini-table th { color: var(--faint); font-size: 11px; font-weight: 500; }
  .mini-table td.src { color: var(--dim); font-size: 11px; }
  .state-line { font-size: 12.5px; color: var(--dim); margin: 6px 0; line-height: 1.8; }
  .state-line b { color: var(--text); font-weight: 600; }
  .state-line .off { color: var(--faint); }
  .ok-tag { color: #128a5b; } .bad-tag { color: #e5484d; }
  #toast {
    position: fixed; top: 18px; right: 22px; z-index: 999;
    background: #101828; color: #fff; font-size: 12.5px; line-height: 1.5;
    border-radius: 10px; padding: 9px 14px; box-shadow: 0 6px 20px rgba(16,24,40,.18);
    opacity: 0; transform: translateY(-6px); transition: opacity .18s ease, transform .18s ease;
    pointer-events: none; max-width: 60vw;
  }
  #toast.show { opacity: 1; transform: none; }
  #toast.ok { background: #128a5b; }
  #toast.err { background: #e5484d; }
  #toast.saving { background: #2e6fe8; }
</style>
</head>
<body>
<div class="topbar">
  <span class="hex">⬡</span>
  <span class="crumb">Kimi Board &nbsp;›&nbsp; <b>设置</b></span>
  <span class="spacer"></span>
  <a class="back" href="/">← 返回看板</a>
</div>
<div id="toast"></div>

<div class="card">
  <div class="sec-head"><span class="hex">⬡</span>会员档位 <span class="sec-desc">套餐月费（元），用于计算回本率</span></div>
  <div class="row">
    <label>档位模式</label>
    <span class="radio">
      <label><input type="radio" name="planMode" value="auto"> 自动识别（kimi web 在线时）</label>
      <label><input type="radio" name="planMode" value="tier"> 指定档位</label>
      <label><input type="radio" name="planMode" value="custom"> 自定义月费</label>
    </span>
  </div>
  <div class="row" id="rowTier">
    <label>档位</label>
    <select id="planTier"></select>
    <span class="hint" id="tierPrice"></span>
  </div>
  <div class="row" id="rowCustom">
    <label>月费（元）</label>
    <input type="number" id="planPrice" min="0" step="0.01" class="wide">
  </div>
  <div class="state-line" id="planState"></div>
</div>

<div class="card">
  <div class="sec-head"><span class="hex">⬡</span>计费周期 <span class="sec-desc">"本账期" 的起算点，精确到分钟</span></div>
  <div class="row">
    <label>每月起算日</label>
    <input type="number" id="cycleDay" min="1" max="31" step="1">
    <span class="hint">日（超出当月天数时按当月最后一天）</span>
  </div>
  <div class="row">
    <label>起算时刻</label>
    <input type="number" id="cycleHour" min="0" max="23" step="1"> <span>时</span>
    <input type="number" id="cycleMinute" min="0" max="59" step="1"> <span>分</span>
    <span class="hint">例如：会员从每月 1 日 00:00 开始，保持默认即可</span>
  </div>
</div>

<div class="card">
  <div class="sec-head"><span class="hex">⬡</span>价目表 <span class="sec-desc">估算等效 API 费用的单价（元 / 1M tokens）</span></div>
  <div class="row">
    <label>价格来源</label>
    <select id="priceSource">
      <option value="kimi">Kimi 官方刊例（platform.kimi.com，元）</option>
      <option value="modelsdev">models.dev（USD，按汇率折元）</option>
      <option value="manual">手动（仅用下方覆盖值 + 内置兜底）</option>
    </select>
  </div>
  <div class="row" id="rowUsdCny">
    <label>USD → CNY 汇率</label>
    <input type="number" id="usdCny" min="0" step="0.001" class="wide">
    <span class="hint">留空 = 自动获取；自动获取失败时按 7.25 估算</span>
  </div>
  <div class="row">
    <label>k3-256k 计价</label>
    <span class="radio">
      <label><input type="checkbox" id="k3half"> 按 k3 半价计算（官方称约为一半，口径未知）</label>
    </span>
  </div>
  <div class="price-grid" id="overrideGrid" style="display:none">
    <div class="pg-head"><span>模型</span><span>缓存命中</span><span>输入</span><span>输出</span></div>
    <div class="pg-row"><span class="pn">kimi-code/k3</span><input type="number" step="0.01" data-model="kimi-code/k3" data-k="0"><input type="number" step="0.01" data-model="kimi-code/k3" data-k="1"><input type="number" step="0.01" data-model="kimi-code/k3" data-k="2"></div>
    <div class="pg-row"><span class="pn">kimi-code/k3-256k</span><input type="number" step="0.01" data-model="kimi-code/k3-256k" data-k="0"><input type="number" step="0.01" data-model="kimi-code/k3-256k" data-k="1"><input type="number" step="0.01" data-model="kimi-code/k3-256k" data-k="2"></div>
    <div class="pg-row"><span class="pn">kimi-code/kimi-for-coding-highspeed</span><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding-highspeed" data-k="0"><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding-highspeed" data-k="1"><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding-highspeed" data-k="2"></div>
    <div class="pg-row"><span class="pn">kimi-code/kimi-for-coding</span><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding" data-k="0"><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding" data-k="1"><input type="number" step="0.01" data-model="kimi-code/kimi-for-coding" data-k="2"></div>
  </div>
  <div class="state-line" id="overrideHint" style="display:none">手动覆盖（仅 manual 源显示）：三项都填才生效，留空用内置兜底。<span id="priceSrcInfo"></span></div>
  <div class="actions">
    <button class="btn ghost" id="btnSyncPrice">立即同步价格</button>
  </div>
  <div class="mini-table-wrap"><table class="mini-table" id="priceTable"></table></div>
</div>

<div class="card">
  <div class="sec-head"><span class="hex">⬡</span>官方限额 <span class="sec-desc">官网百分比(两位) → KimiCode(整数) 自动回退</span></div>
  <div class="row">
    <label>同步方式</label>
    <span class="radio">
      <label><input type="radio" name="subMode" value="webview"> WebView 登录</label>
      <label><input type="radio" name="subMode" value="extension"> 浏览器扩展</label>
      <label><input type="radio" name="subMode" value="manual"> 手动 Token（救援）</label>
    </span>
  </div>
  <div class="actions">
    <button class="btn" id="btnConnect">连接 Kimi</button>
    <button class="btn ghost" id="btnSyncSub">立即同步</button>
    <a class="btn ghost" id="btnOpenSite" href="https://www.kimi.com/membership/subscription?tab=quota" target="_blank" rel="noopener" style="text-decoration:none">打开官网配额页</a>
  </div>
  <div class="state-line" id="subState"></div>
  <div class="row" id="rowSubToken">
    <label>官网 Token（JWT）</label>
    <input type="text" id="subToken" placeholder="浏览器 kimi-auth cookie 的值（eyJ…）；仅在内存 / 系统凭据库" style="width:100%; flex:1">
  </div>
  <div class="row" id="rowPersistToken">
    <label>保存方式</label>
    <span class="radio">
      <label><input type="checkbox" id="persistToken"> 保存到系统凭据库（Windows 凭据管理器）</label>
    </span>
    <span class="hint">默认不持久化，重启后需重新粘贴；凭据库只在保存时才写入</span>
  </div>
  <div class="row">
    <label>官网不可用时的兜底数据源</label>
    <select id="quotaSource">
      <option value="auto">自动（优先本机 kimi web，失败直连云端）</option>
      <option value="local">仅本机 kimi web</option>
      <option value="cloud">仅云端 API</option>
    </select>
    <button class="btn ghost" id="btnSyncQuota">同步</button>
  </div>
  <div class="state-line" id="quotaState"></div>
  <div class="row">
    <label>退出登录</label>
    <button class="btn ghost" id="btnLogout">清除 Kimi 登录数据（WebView Cookie / 存储 / 看板缓存）</button>
  </div>
</div>

<div class="actions">
  <button class="btn" id="btnSave">立即保存</button>
  <button class="btn ghost" id="btnReset">恢复默认</button>
  <span class="spacer"></span>
  <span class="state-line" id="configPath"></span>
</div>
<div id="status"></div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STATE = { pricing: null, quota: null };
const KB_SECRET = (document.querySelector('meta[name="kb-secret"]') || {}).content || "";
const kh = () => ({ "X-KB-Secret": KB_SECRET, "Content-Type": "application/json" });

function fillPlan(st) {
  const p = st.config.plan;
  const mode = p.price != null ? "custom" : p.tier ? "tier" : "auto";
  document.querySelectorAll("input[name=planMode]").forEach(r => r.checked = r.value === mode);
  $("planPrice").value = p.price != null ? p.price : "";
  const tierSel = $("planTier");
  tierSel.innerHTML = Object.entries(st.planPrices || {})
    .map(([k, v]) => `<option value="${esc(k)}">${esc(k)} — ¥${v}</option>`).join("");
  tierSel.value = p.tier || "";
  updatePlanMode();
}

function updatePlanMode() {
  const mode = document.querySelector("input[name=planMode]:checked").value;
  $("rowTier").style.display = mode === "tier" ? "" : "none";
  $("rowCustom").style.display = mode === "custom" ? "" : "none";
  $("tierPrice").textContent = $("planTier").selectedIndex >= 0 && $("planTier").options.length
    ? `→ ¥${$("planTier").options[$("planTier").selectedIndex].text.split("—")[1] || ""}`
    : "";
}
document.querySelectorAll("input[name=planMode]").forEach(r => r.onchange = updatePlanMode);
$("planTier").onchange = updatePlanMode;

function fill(st) {
  const c = st.config;
  $("cycleDay").value = c.billing.day;
  $("cycleHour").value = c.billing.hour;
  $("cycleMinute").value = c.billing.minute;
  $("priceSource").value = c.pricing.source;
  $("usdCny").value = c.pricing.usdCny != null ? c.pricing.usdCny : "";
  $("k3half").checked = !!c.pricing.k3half;
  $("quotaSource").value = c.quota.source;
  const sm = c.subscription.source === "manual" ? "manual"
    : (st.subscription && st.subscription.source === "extension" ? "extension" : "webview");
  document.querySelectorAll("input[name=subMode]").forEach(r => r.checked = r.value === sm);
  $("subToken").value = "";
  $("persistToken").checked = !!c.subscription.persistToken;
  document.querySelectorAll("#overrideGrid input[data-model]").forEach(inp => {
    const ov = (c.pricing.overrides || {})[inp.dataset.model];
    inp.value = ov && ov[+inp.dataset.k] != null ? ov[+inp.dataset.k] : "";
  });
  fillPlan(st);
  $("rowUsdCny").style.display = $("priceSource").value === "modelsdev" ? "" : "none";
  updatePriceMode();
  updateSubMode();
  renderStatus(st);
}

function updateSubMode() {
  const manual = document.querySelector("input[name=subMode]:checked").value === "manual";
  $("rowSubToken").style.display = manual ? "" : "none";
  $("rowPersistToken").style.display = manual ? "" : "none";
}
document.querySelectorAll("input[name=subMode]").forEach(r => r.addEventListener("change", updateSubMode));

function updatePriceMode() {
  const m = $("priceSource").value === "manual";
  $("overrideGrid").style.display = m ? "" : "none";
  $("overrideHint").style.display = m ? "" : "none";
}

function renderStatus(st) {
  const p = st.pricing, q = st.quota, s = st.subscription;
  STATE.pricing = p; STATE.quota = q;
  $("planState").innerHTML = `当前套餐：<b>¥${st.plan.price}</b>${st.plan.name ? " · " + esc(st.plan.name) : ""} · 来源 ${esc(st.plan.source)}`;
  $("configPath").textContent = "配置文件：" + esc(st.configPath);
  $("priceSrcInfo").textContent = p.ok ? ` · 来源已生效：${esc(p.message)}` : ` · ${esc(p.message)}`;
  const rows = Object.entries(p.table || {}).map(([m, v]) =>
    `<tr><td>${esc(m)}</td><td>${v[0]}</td><td>${v[1]}</td><td>${v[2]}</td></tr>`).join("");
  $("priceTable").innerHTML = `<tr><th>生效价目（元/1M）</th><th>缓存</th><th>输入</th><th>输出</th></tr>` + rows;
  $("rowUsdCny").style.display = $("priceSource").value === "modelsdev" ? "" : "none";
  if (q) {
    $("quotaState").innerHTML = q.enabled
      ? (q.ok
        ? `已同步（<b class="ok-tag">${q.source === "cloud" ? "云端" : "本地"}</b>）· ${(q.rows || []).length} 条限额`
        : `<b class="bad-tag">同步失败</b> · ${esc(q.message || "")}`)
      : '<span class="off">官方限额同步已关闭</span>';
  }
  if (s) {
    const srcTxt = {manual: "手动 Token", webview: "WebView 登录", extension: "浏览器扩展"}[s.source] || s.source;
    const tokTxt = st.manualTokenSet ? " · 已设置 Token（不落盘）" : "";
    const bgTxt = st.webviewActive ? ' · <b class="ok-tag">后台实时刷新中</b>' : "";
    $("subState").innerHTML = !s.enabled
      ? '<span class="off">月额度同步已关闭</span>'
      : s.ok
        ? `已同步（<b class="ok-tag">${srcTxt}</b>）${s.fetchedAt ? " · " + new Date(s.fetchedAt).toLocaleString("zh-CN", {hour12: false}) : ""}${s.message ? " · " + esc(s.message) : ""}${tokTxt}${bgTxt}`
        : `<b class="bad-tag">暂无数据</b> · ${esc(s.message || "")}${tokTxt}`;
  }
}

async function load() {
  const st = await (await fetch("/api/settings", {cache: "no-store"})).json();
  fill(st);
}
let saveTimer = null;
function showToast(msg, cls) {
  const t = $("toast");
  t.textContent = msg;
  t.className = cls || "";
  void t.offsetWidth;
  t.classList.add("show");
  clearTimeout(showToast._h);
  showToast._h = setTimeout(() => t.classList.remove("show"), 1800);
}
function setStatus(msg, cls) { const s = $("status"); s.textContent = msg; s.className = cls; }

async function save() {
  const mode = document.querySelector("input[name=planMode]:checked").value;
  const cfg = {
    plan: {
      auto: mode === "auto",
      tier: mode === "tier" ? $("planTier").value : "",
      price: mode === "custom" ? Number($("planPrice").value) : null,
    },
    billing: {
      day: Math.min(31, Math.max(1, Number($("cycleDay").value) || 1)),
      hour: Math.min(23, Math.max(0, Number($("cycleHour").value) || 0)),
      minute: Math.min(59, Math.max(0, Number($("cycleMinute").value) || 0)),
    },
    pricing: {
      source: $("priceSource").value,
      usdCny: $("usdCny").value ? Number($("usdCny").value) : null,
      k3half: $("k3half").checked,
      overrides: {},
    },
    quota: { source: $("quotaSource").value },
    subscription: {
      enabled: true,
      source: document.querySelector("input[name=subMode]:checked").value === "manual" ? "manual" : "auto",
      persistToken: $("persistToken").checked,
      token: $("subToken").value.trim(),
    },
  };
  const overrides = {};
  document.querySelectorAll("#overrideGrid input[data-model]").forEach(inp => {
    (overrides[inp.dataset.model] = overrides[inp.dataset.model] || [])[+inp.dataset.k] = inp.value.trim();
  });
  Object.keys(overrides).forEach(m => {
    const arr = overrides[m];
    if (!(arr.length === 3 && arr.every(v => v !== "" && isFinite(Number(v)) && Number(v) >= 0))) delete overrides[m];
    else overrides[m] = arr.map(Number);
  });
  cfg.pricing.overrides = overrides;
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: kh(),
      body: JSON.stringify(cfg),
    });
    const st = await res.json();
    if (res.ok) {
      renderStatus(st);
      showToast("已保存 ✓", "ok");
    } else {
      showToast("保存失败：" + (st.error || "未知错误"), "err");
    }
  } catch (e) {
    showToast("保存失败：" + e, "err");
  }
}
function scheduleSave() {
  clearTimeout(saveTimer);
  showToast("修改中…", "saving");
  saveTimer = setTimeout(save, 600);
}
// 自动保存：任何设置项变更后防抖保存
document.querySelectorAll("input[name=planMode]").forEach(r => r.addEventListener("change", scheduleSave));
["planTier", "planPrice", "cycleDay", "cycleHour", "cycleMinute", "priceSource", "usdCny", "k3half",
 "quotaSource", "persistToken", "subToken"].forEach(id => {
  const el = $(id);
  if (el) {
    el.addEventListener("input", scheduleSave);
    el.addEventListener("change", scheduleSave);
  }
});
document.querySelectorAll("input[name=subMode]").forEach(r => r.addEventListener("change", scheduleSave));
document.querySelectorAll("#overrideGrid input[data-model]").forEach(inp => inp.addEventListener("input", scheduleSave));
$("btnSave").onclick = () => { clearTimeout(saveTimer); save(); };
$("btnReset").onclick = async () => {
  if (!window.confirm("确定要恢复默认设置吗？\\n会员档位、计费周期、价目来源、月额度等所有配置都将重置。")) return;
  clearTimeout(saveTimer);
  const res = await fetch("/api/settings", {method: "POST", headers: kh(), body: "{}"});
  const st = await res.json();
  setStatus("已恢复默认设置。", "ok");
  fill(st);
  showToast("已恢复默认设置 ✓", "ok");
};
$("btnSyncPrice").onclick = async () => {
  $("btnSyncPrice").disabled = true;
  showToast("正在同步价格…", "saving");
  try {
    const st = await (await fetch("/api/settings?syncPrice=1", {cache: "no-store"})).json();
    renderStatus(st);
    if (st.pricing.ok) { showToast("价格同步成功 ✓ 来源：" + st.pricing.message, "ok"); setStatus("价格同步完成。", "ok"); }
    else { showToast("价格同步失败：" + st.pricing.message, "err"); setStatus("价格同步失败：" + st.pricing.message, "err"); }
  } catch (e) {
    showToast("价格同步失败：" + e, "err");
  }
  $("btnSyncPrice").disabled = false;
};
$("btnSyncQuota").onclick = async () => {
  $("btnSyncQuota").disabled = true;
  const st = await (await fetch("/api/settings?syncQuota=1", {cache: "no-store"})).json();
  renderStatus(st);
  setStatus("配额同步完成。", st.quota.ok ? "ok" : "err");
  $("btnSyncQuota").disabled = false;
};
$("btnConnect").onclick = async () => {
  $("btnConnect").disabled = true;
  setStatus("正在启动登录窗口…");
  try {
    const r = await (await fetch("/api/connect", {method: "POST", headers: kh()})).json();
    setStatus(r.message, r.ok ? "ok" : "err");
  } catch (e) {
    setStatus("启动失败：" + e, "err");
  }
  $("btnConnect").disabled = false;
};
$("btnSyncSub").onclick = async () => {
  $("btnSyncSub").disabled = true;
  setStatus("正在同步月额度…");
  try {
    const body = JSON.stringify({subscription: {enabled: true, source: document.querySelector("input[name=subMode]:checked").value === "manual" ? "manual" : "auto", persistToken: $("persistToken").checked, token: $("subToken").value.trim()}});
    const r = await (await fetch("/api/settings", {method: "POST", headers: kh(), body: body})).json();
    renderStatus(r);
    const st = await (await fetch("/api/settings?syncQuota=1", {cache: "no-store"})).json();
    renderStatus(st);
    setStatus(st.subscription.ok ? "月额度已同步（" + st.subscription.source + "）。" : "暂未收到月额度：" + st.subscription.message, st.subscription.ok ? "ok" : "err");
  } catch (e) {
    setStatus("同步失败：" + e, "err");
  }
  $("btnSyncSub").disabled = false;
};
$("btnLogout").onclick = async () => {
  $("btnLogout").disabled = true;
  setStatus("正在清除 Kimi 登录数据…");
  try {
    const r = await (await fetch("/api/logout-kimi", {method: "POST", headers: kh()})).json();
    setStatus(r.message || "已清除", "ok");
    const st = await (await fetch("/api/settings", {cache: "no-store"})).json();
    renderStatus(st);
  } catch (e) {
    setStatus("清除失败：" + e, "err");
  }
  $("btnLogout").disabled = false;
};
$("priceSource").onchange = () => { $("rowUsdCny").style.display = $("priceSource").value === "modelsdev" ? "" : "none"; updatePriceMode(); };

// 状态区每 8 秒自动刷新（登录/后台刷新后无需手动刷新页面）
async function refreshState() {
  if (document.hidden) return;
  try {
    const st = await (await fetch("/api/settings", {cache: "no-store"})).json();
    renderStatus(st);
  } catch (e) { /* 瞬时失败忽略 */ }
}
setInterval(refreshState, 8000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });

load();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- 服务


def normalize_config(raw: dict) -> dict:
    """校验并归一化前端 / CLI 提交的配置。"""
    base = default_config()
    raw = raw or {}
    plan = raw.get("plan") or {}
    billing = raw.get("billing") or {}
    pricing = raw.get("pricing") or {}
    quota = raw.get("quota") or {}
    subscription = raw.get("subscription") or {}
    overrides = {}
    for m, v in (pricing.get("overrides") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 3:
            try:
                overrides[m] = [max(0.0, float(x)) for x in v]
            except (TypeError, ValueError):
                pass
    return {
        "version": 1,
        "plan": {
            "auto": bool(plan.get("auto", base["plan"]["auto"])),
            "tier": str(plan.get("tier", "") or ""),
            "price": (None if plan.get("price") in (None, "", "null")
                      else float(plan["price"])),
        },
        "billing": {
            "day": min(31, max(1, int(billing.get("day", 1) or 1))),
            "hour": min(23, max(0, int(billing.get("hour", 0) or 0))),
            "minute": min(59, max(0, int(billing.get("minute", 0) or 0))),
        },
        "pricing": {
            "source": pricing.get("source", "kimi") if pricing.get("source") in ("kimi", "modelsdev", "manual") else "kimi",
            "usdCny": (None if pricing.get("usdCny") in (None, "", 0, "null")
                       else float(pricing["usdCny"])),
            "overrides": overrides,
            "k3half": bool(pricing.get("k3half", base["pricing"].get("k3half", False))),
        },
        "quota": {
            "enabled": bool(quota.get("enabled", base["quota"]["enabled"])),
            "source": quota.get("source", "auto") if quota.get("source") in ("auto", "local", "cloud") else "auto",
        },
        "subscription": {
            "enabled": bool(subscription.get("enabled", base["subscription"]["enabled"])),
            "source": subscription.get("source", "auto") if subscription.get("source") in ("auto", "manual") else "auto",
            "persistToken": bool(subscription.get("persistToken", base["subscription"].get("persistToken", False))),
        },
    }


def apply_config(cfg: dict) -> None:
    """应用配置并重置价格 / 配额缓存。"""
    global CFG
    CFG = normalize_config(cfg)
    refresh_pricing(force=True)
    with _quota_lock:
        _quota_cache.update(at=0.0, data=None, busy=False)


def settings_state(sync_price=False, sync_quota=False) -> dict:
    cfg = CFG
    if sync_price:
        refresh_pricing(force=True)
    plan_price, plan_name, plan_is_auto, plan_src = resolve_plan(cfg)
    return {
        "config": cfg,
        "configPath": str(config_path()),
        "plan": {"price": plan_price, "name": plan_name, "auto": plan_is_auto, "source": plan_src},
        "pricing": pricing_info(),
        "quota": quota_snapshot(cfg, force=sync_quota),
        "subscription": subscription_snapshot(cfg, force=sync_quota),
        "manualTokenSet": bool(get_manual_token(cfg)),
        "webviewActive": _webview_active,
        "fxRate": usd_cny_rate(cfg)[0],
        "planPrices": PLAN_PRICES,
    }


class Handler(BaseHTTPRequestHandler):
    # ---- 本地接口防护：Host 回环校验 + Origin 白名单 + 随机 secret ----
    @staticmethod
    def _host_ok(host: str) -> bool:
        h = (host or "").split(":")[0].strip("[]")
        return h in ("127.0.0.1", "localhost", "::1") or h.endswith(".localhost")

    def _origin(self):
        return self.headers.get("Origin") or ""

    def _origin_allowed(self):
        """返回 None=拒绝；'same'/'local'=放行；'ext'=需校验 secret。"""
        origin = self._origin()
        if not origin:
            return "local"  # 非浏览器来源（本地脚本/进程）
        host = self.headers.get("Host") or ""
        if origin == f"http://{host}":
            return "same"
        if _LOCAL_ORIGIN_RE.match(origin):
            return "local"
        if origin.startswith("chrome-extension://"):
            return "ext"
        return None

    def _authorized(self):
        if not self._host_ok(self.headers.get("Host") or ""):
            return False
        oa = self._origin_allowed()
        if oa is None:
            return False
        if oa == "ext" and self.headers.get("X-KB-Secret") != _local_secret:
            return False
        return True

    def _deny(self, code=403):
        self._send(code, "text/plain", b"forbidden")

    def do_OPTIONS(self):
        oa = self._origin_allowed()
        if oa is None or not self._host_ok(self.headers.get("Host") or ""):
            self._deny()
            return
        self.send_response(204)
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-KB-Secret")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._deny()
            return
        path = urllib.parse.urlsplit(self.path)
        if path.path in ("/", "/index.html"):
            body = INDEX_HTML.replace("__KB_SECRET__", _local_secret).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path.path == "/settings":
            body = SETTINGS_HTML.replace("__KB_SECRET__", _local_secret).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path.path == "/favicon.png":
            if FAVICON_BYTES:
                self._send(200, "image/png", FAVICON_BYTES)
            else:
                self._send(404, "text/plain", b"no favicon")
        elif path.path.startswith("/api/stats"):
            body = json.dumps(collect_stats(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/settings"):
            q = urllib.parse.parse_qs(path.query)
            st = settings_state(sync_price="syncPrice" in q, sync_quota="syncQuota" in q)
            body = json.dumps(st, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/quota"):
            q = urllib.parse.parse_qs(path.query)
            body = json.dumps(quota_snapshot(CFG, force="refresh" in q), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/subscription"):
            q = urllib.parse.parse_qs(path.query)
            body = json.dumps(subscription_snapshot(CFG, force="refresh" in q), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/connect"):
            body = json.dumps(_open_connect(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/version"):
            body = json.dumps(check_update(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path == "/api/debug":
            dbg = dict(_webview_dbg,
                       webviewActive=_webview_active,
                       webviewSession=session_active(),
                       subscription=subscription_snapshot(CFG))
            body = json.dumps(dbg, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/api/settings":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                self._send(400, "application/json; charset=utf-8",
                           b'{"error":"invalid json"}')
                return
            try:
                # 手动 Token：只在内存/系统凭据库，不进配置文件
                raw_sub = raw.get("subscription") or {}
                set_manual_token(raw_sub.get("token", ""), bool(raw_sub.get("persistToken")))
                cfg = normalize_config(raw)
                save_config(cfg)
                apply_config(cfg)
            except Exception as e:
                self._send(400, "application/json; charset=utf-8",
                           json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
                return
            body = json.dumps(settings_state(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path.path.startswith("/api/subscription"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            except Exception:
                raw = "{}"
            q = urllib.parse.parse_qs(path.query)
            source = (q.get("source") or ["auto"])[0]
            resp = handle_subscription_post(raw, source)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        elif path.path == "/api/connect":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(_open_connect(), ensure_ascii=False).encode("utf-8"))
        elif path.path == "/api/logout-kimi":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(clear_kimi_login(), ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 只对白名单来源回显 ACAO，杜绝任意网页读取本地数据
        origin = self._origin()
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 浏览器提前关闭连接，属正常噪音

    def log_message(self, *args):
        pass


class QuietHTTPServer(ThreadingHTTPServer):
    """吞掉客户端中断连接的异常，避免刷屏。"""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


def main():
    global CFG, SERVER_PORT, _connect_queue
    import queue

    ap = argparse.ArgumentParser(description="kimi code token 消耗网页看板")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--plan-price", type=float, default=None,
                    help="会员月费（元），覆盖配置文件；默认自动识别，失败按 199 计")
    ap.add_argument("--plan-tier", default=None,
                    help="会员档位名（adagio/andante/moderato/allegretto/allegro），覆盖配置")
    ap.add_argument("--price-source", default=None,
                    choices=("kimi", "modelsdev", "manual"),
                    help="价目来源：kimi=官方刊例(元) modelsdev=models.dev(USD折元) manual=手动")
    ap.add_argument("--cycle-day", type=int, default=None, help="计费周期每月起算日（1-31）")
    ap.add_argument("--cycle-hour", type=int, default=None, help="计费周期起算小时（0-23）")
    ap.add_argument("--cycle-minute", type=int, default=None, help="计费周期起算分钟（0-59）")
    ap.add_argument("--usd-cny", type=float, default=None, help="USD→CNY 汇率（modelsdev 来源时）")
    ap.add_argument("--k3-256k-half", action="store_true",
                    help="kimi-code/k3-256k 按 k3 生效价的 50% 计价（默认与 k3 同价）")
    ap.add_argument("--no-quota", action="store_true", help="关闭官方限额同步")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    cfg = load_config()
    if args.plan_price is not None:
        cfg["plan"]["price"] = args.plan_price
    if args.plan_tier:
        cfg["plan"]["tier"] = args.plan_tier
    if args.price_source:
        cfg["pricing"]["source"] = args.price_source
    if args.cycle_day is not None:
        cfg["billing"]["day"] = args.cycle_day
    if args.cycle_hour is not None:
        cfg["billing"]["hour"] = args.cycle_hour
    if args.cycle_minute is not None:
        cfg["billing"]["minute"] = args.cycle_minute
    if args.usd_cny is not None:
        cfg["pricing"]["usdCny"] = args.usd_cny
    if args.k3_256k_half:
        cfg["pricing"]["k3half"] = True
    if args.no_quota:
        cfg["quota"]["enabled"] = False
    CFG = normalize_config(cfg)

    # 用上次快照预填价目，首屏即显示正确价格；随后后台抓取更新
    _seed_price_cache()

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

    # 后台预热：首屏进入前先把价格/配额抓回来，避免首个请求卡网络
    threading.Thread(target=lambda: (refresh_pricing(force=True),
                                     fetch_quota(CFG, force=True)),
                     daemon=True).start()

    server = QuietHTTPServer((args.host, args.port), Handler)
    SERVER_PORT = args.port
    url = f"http://{args.host}:{args.port}"
    print(f"kimi code token 看板已启动：{url}  (Ctrl+C 停止)")
    print(f"  设置页：{url}/settings · 配置文件：{config_path()}")
    if not args.no_open:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass

    # HTTP 服务放后台线程；主线程专跑"连接 Kimi"的 WebView（pywebview 需主线程）
    _connect_queue = queue.Queue()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    # 有持久 WebView 会话时，启动即后台自动重连（隐藏窗口实时刷新）
    if webview_available() and session_active():
        _connect_queue.put_nowait("connect")
        print("  检测到持久 Kimi 会话，后台自动刷新月额度中…")
    try:
        while True:
            _connect_queue.get()
            run_connect_webview(args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
