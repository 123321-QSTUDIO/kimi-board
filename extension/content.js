// Kimi Board 浏览器组件：
//  右侧面板：一整条，三个圆形进度条显示 月/5h/周 限额，圆下标注限额名 + 重置时间；
//            下方显示 本账期用量 + 等效 API 费用。
//  侧栏小条（原始样式）：本月已用 token 数，点击打开看板。
// 数据来自本机看板 /api/stats（含 cc-switch 合并）；右侧面板自动跟随 WebUI 亮/暗主题。
// WebUI 是 SPA，用 MutationObserver 保证组件被移除后重新挂载。
(() => {
  const DASH = "http://127.0.0.1:8321";
  const FONT = '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
  const MONO = 'ui-monospace, Consolas, "Cascadia Mono", monospace';

  // ---- 主题：跟随 WebUI 亮/暗（用于右侧面板） ----
  const vars = {
    light: {
      "--kb-bg": "#ffffff", "--kb-line": "#e3e9f4", "--kb-text": "#101828",
      "--kb-dim": "#5d6b82", "--kb-faint": "#a8b4cc", "--kb-blue": "#2e6fe8",
      "--kb-soft": "#f2f6fd", "--kb-red": "#e5484d", "--kb-orange": "#e8833a",
      "--kb-bar1": "#5ea2ff", "--kb-bar2": "#9ec4ff", "--kb-bar3": "#b9d6fc",
    },
    dark: {
      "--kb-bg": "#1a1f2b", "--kb-line": "#2c3444", "--kb-text": "#e6eaf2",
      "--kb-dim": "#9aa4b8", "--kb-faint": "#6b7488", "--kb-blue": "#5ea2ff",
      "--kb-soft": "#232a39", "--kb-red": "#ff6b70", "--kb-orange": "#ff9d4d",
      "--kb-bar1": "#5ea2ff", "--kb-bar2": "#7fa8e8", "--kb-bar3": "#5874a8",
    },
  };
  const root = document.documentElement;
  function detectDark() {
    const el = document.documentElement;
    const byClass = el.classList.contains("dark") || el.classList.contains("theme-dark");
    const byAttr = (el.getAttribute("data-theme") || "").includes("dark");
    const byMedia = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return byClass || byAttr || byMedia;
  }
  let onTheme = null;  // 主题变化回调（小条状态灯重上色）
  function applyTheme() {
    const dark = detectDark();
    const t = vars[dark ? "dark" : "light"];
    for (const [k, v] of Object.entries(t)) root.style.setProperty(k, v);
    if (onTheme) onTheme();
  }
  for (const [k, v] of Object.entries(vars.light)) root.style.setProperty(k, v);

  // ---- 数据 ----
  let alive = false;
  let curLogoLevel = "ok";  // 当前状态灯级别，主题切换时重上色

  function fmtCN(n) {
    if (n >= 1e8) return (n / 1e8).toFixed(2).replace(/\.?0+$/, "") + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, "") + "万";
    return String(Math.round(n));
  }
  function fmtTok(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return String(Math.round(n));
  }
  function fmtPct(v) {
    if (v == null) return "--";
    const n = +(+v).toFixed(2);
    return (n % 1 === 0 ? n : n.toFixed(2).replace(/\.?0+$/, "")) + "%";
  }
  const yuan = n => "¥" + (n >= 10000 ? (n / 10000).toFixed(2).replace(/\.?0+$/, "") + "万" : n.toFixed(n < 10 ? 2 : 0));

  async function fetchStats() {
    const d = await (await fetch(DASH + "/api/stats", { cache: "no-store" })).json();
    alive = true;
    return d;
  }

  // ---- 侧栏小条：图标(状态灯) + 限额轮播/通知 + 箭头；保持侧栏内嵌布局 ----
  const btn = document.createElement("div");
  btn.id = "kimi-board-btn";
  Object.assign(btn.style, {
    display: "flex", alignItems: "baseline", gap: "7px",
    margin: "2px 12px 8px", padding: "8px 12px", borderRadius: "10px",
    background: "var(--kb-bg)", border: "1px solid var(--kb-line)",
    fontFamily: FONT, cursor: "pointer", userSelect: "none",
    transition: "border-color .15s, color .2s",
  });
  // hover 只轻微加深背景，不改边框色（边框色由状态灯 setLogo 控制）
  btn.onmouseenter = () => (btn.style.background = "var(--kb-soft)");
  btn.onmouseleave = () => (btn.style.background = "var(--kb-bg)");

  // 状态灯：复用现有六边形图标，颜色随限额状态变（蓝=正常 / 黄=warn / 红=danger / 绿=恢复）
  const icon = document.createElement("span");
  icon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round" aria-hidden="true"><polygon points="12,2.6 20.3,7.4 20.3,16.6 12,21.4 3.7,16.6 3.7,7.4"/></svg>';
  Object.assign(icon.style, {
    display: "inline-flex", alignItems: "center", lineHeight: "0",
    alignSelf: "center", flexShrink: "0", color: "var(--kb-blue)",
  });

  // 状态灯颜色（亮/暗主题下各自取色）
  const LIGHT = { ok: "#2e6fe8", warn: "#e8833a", danger: "#e5484d", recovered: "#19b562" };
  const DARK = { ok: "#5ea2ff", warn: "#ff9d4d", danger: "#ff6b70", recovered: "#34d47e" };
  function setLogo(level) {
    stopBlink();
    curLogoLevel = level;
    const palette = detectDark() ? DARK : LIGHT;
    icon.style.transition = "color .2s ease";
    icon.style.color = palette[level] || LIGHT.ok;
    btn.style.borderColor = palette[level] || LIGHT.ok;
  }
  onTheme = () => {
    const palette = detectDark() ? DARK : LIGHT;
    icon.style.color = palette[curLogoLevel] || LIGHT.ok;
    btn.style.borderColor = palette[curLogoLevel] || LIGHT.ok;
  };

  const label = document.createElement("span");
  label.textContent = "本月";
  Object.assign(label.style, {
    fontSize: "11.5px", color: "var(--kb-dim)", lineHeight: "1",
    whiteSpace: "nowrap", flexShrink: "1",
  });

  const arrow = document.createElement("span");
  arrow.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="var(--kb-faint)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  Object.assign(arrow.style, { display: "inline-flex", alignItems: "center", lineHeight: "0", marginLeft: "auto", alignSelf: "center", flexShrink: "0" });

  btn.append(icon, label, arrow);

  // 距离重置剩余时间
  function resetIn(resetTime) {
    if (!resetTime) return "";
    const ms = new Date(resetTime).getTime() - Date.now();
    if (isNaN(ms) || ms <= 0) return "";
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d} 天 ${h} 小时`;
    if (h > 0) return `${h} 时 ${m} 分`;
    return `${m} 分`;
  }
  function shortName(r) {
    if (!r) return "";
    if (r.name.includes("月")) return "月额度";
    if (r.name.includes("周")) return "周限额";
    if (r.name.includes("5")) return "5 小时限额";
    return r.name;
  }
  function levelOf(used) { return used >= 95 ? "danger" : used >= 80 ? "warn" : "ok"; }

  const btnState = { prevLevel: "ok", prevR: null, timer: null };
  const carousel = { items: [], idx: 0, timer: null, running: false };

  // 文本淡入淡出切换：淡出→换文本→淡入，完成后回调
  const FADE = 1000;  // ms 渐入渐出时长
  // 数值/百分比/金额/时长加粗亮色（纯标签文字保持灰色）
  function em(text) {
    return String(text || "").replace(
      /(\d+(?:\.\d+)?%\s*tokens?|\d+(?:\.\d+)?\s*[亿万KMB]\s*tokens?|¥\d+(?:\.\d+)?|\d+(?:\.\d+)?%|\d+\s*天\s*\d+\s*小时|\d+\s*时\s*\d+\s*分|\d+\s*小时\s*\d+\s*分|\d+\s*分)/g,
      '<b style="font-weight:700;color:var(--kb-text)">$1</b>');
  }
  function fadeLabel(text, done) {
    label.style.transition = "opacity " + FADE + "ms ease";
    label.style.opacity = "0";
    setTimeout(() => {
      label.innerHTML = em(text);
      label.style.opacity = "1";
      setTimeout(done || (() => {}), FADE);
    }, FADE);
  }
  // 状态灯闪烁：在状态色与底色间淡入淡出 n 次；结束后回调
  // 用独立闭包 timer（不用 btnState.blinkTimer），避免被 playSeq/setLogo 清除
  let blinkT = null;
  function stopBlink() {
    if (blinkT) { clearTimeout(blinkT); blinkT = null; }
    // 恢复图标不透明度（1）
    icon.style.transition = "opacity .2s ease";
    icon.style.opacity = "1";
  }
  // 闪烁 = 图标整体淡出再淡入（opacity 0↔1），颜色始终是状态色
  function blinkLogo(level, times, interval, done) {
    stopBlink();
    const palette = detectDark() ? DARK : LIGHT;
    icon.style.transition = "color .2s ease";
    icon.style.color = palette[level] || LIGHT.ok;  // 保持状态色不变
    let count = 0;
    const half = interval / 2;
    const one = (visible) => {
      icon.style.transition = "opacity " + half + "ms ease";
      icon.style.opacity = visible ? "1" : "0";
    };
    const step = () => {
      if (count >= times) {
        one(true);  // 结束恢复完全可见
        if (done) done();
        return;
      }
      one(true);
      blinkT = setTimeout(() => {
        one(false);
        count++;
        blinkT = setTimeout(step, half);
      }, half);
    };
    step();
  }

  // 长文本拆段：超过 maxLen 字符就递归在最近分隔符处拆，确保每段 ≤ maxLen（小条不伸缩）
  const MAX_LEN = 22;
  function seg(text) {
    const t = String(text || "");
    if (t.length <= MAX_LEN) return [t];
    const cut = t.lastIndexOf(" · ", MAX_LEN);
    if (cut > 0) return seg(t.slice(0, cut)).concat(seg(t.slice(cut + 3)));
    const cut2 = t.lastIndexOf(" ", MAX_LEN);
    if (cut2 > 0) return seg(t.slice(0, cut2)).concat(seg(t.slice(cut2 + 1)));
    return seg(t.slice(0, MAX_LEN)).concat(seg(t.slice(MAX_LEN)));
  }

  // 提示序列：每段按自身长度定停留（≤11 字符=3s，>11=5s）；每段消息的"结束位"延长到 6s；
  // 每条消息之间间隔 6s，全部结束后 6s 回轮播。secs[i] 若提供则覆盖该条消息末段时长（如恢复 10s）
  function playSeq(texts, secs) {
    if (btnState.timer) clearTimeout(btnState.timer);
    if (carousel.timer) clearTimeout(carousel.timer);
    carousel.running = false;
    if (!texts.length) { startCarousel(); return; }
    // 每条消息 → 段数组；每段时长按长度，末段 6s（或被 secs 覆盖）
    const msgs = texts.map((t, i) => {
      const parts = seg(t);
      const endSec = (secs && secs[i] != null) ? secs[i] : 6;
      return parts.map((p, pi) => {
        const isLast = pi === parts.length - 1;
        const sec = isLast ? endSec : (p.length > 11 ? 5 : 3);
        return { text: p, sec };
      });
    });
    let mi = 0, pi = 0;
    const showMsg = () => {
      const parts = msgs[mi];
      const showPart = () => {
        if (pi >= parts.length) {
          pi = 0;
          mi++;
          if (mi >= msgs.length) btnState.timer = setTimeout(startCarousel, 6000);  // 全结束，6s 后回轮播
          else btnState.timer = setTimeout(showMsg, 6000);  // 下一条消息前间隔 6s
          return;
        }
        fadeLabel(parts[pi].text, () => {
          pi++;
          btnState.timer = setTimeout(showPart, parts[pi - 1].sec * 1000);
        });
      };
      showPart();
    };
    showMsg();
  }
  function startCarousel() {
    if (btnState.timer) clearTimeout(btnState.timer);
    if (carousel.timer) clearTimeout(carousel.timer);
    stopBlink();
    carousel.running = false;
    if (!carousel.items.length) return;
    carousel.running = true;
    carousel.idx = 0;
    const playItem = () => {
      const item = carousel.items[carousel.idx];
      // 该轮播项若对应某限额，且在提示区域内 → Logo 同步状态色；汇总项恢复默认蓝
      if (item && item.used != null) setLogo(levelOf(item.used));
      else setLogo("ok");
      const parts = seg(item.text);
      let pi = 0;
      const show = () => {
        if (pi >= parts.length) {
          carousel.idx = (carousel.idx + 1) % carousel.items.length;
          carousel.timer = setTimeout(playItem, 10000);
          return;
        }
        fadeLabel(parts[pi], () => { pi++; carousel.timer = setTimeout(show, 10000); });
      };
      show();
    };
    playItem();
  }
  // 轮播内容：5h / 周 / 月限额（只显示剩余）+ 本期 Token 总量 + 本期总花费
  function buildNotices(d) {
    const rows = (d.limits && d.limits.rows) || [];
    const c = d.cost || {};
    const cards = d.cards || {};
    const mt = (cards.month && cards.month.total) || 0;
    const items = [];
    rows.filter(r => r.used != null).forEach(r =>
      items.push({ text: `${shortName(r)} · 剩余 ${(100 - r.used).toFixed(1)}%`, used: r.used }));
    items.push({ text: `本期 ${fmtCN(mt)} tokens`, used: null });
    items.push({ text: `本期 ¥${c.monthTotal != null ? c.monthTotal.toFixed(1) : "--"}`, used: null });
    return items;
  }
  function renderBtnNotify(d) {
    const rows = (d.limits && d.limits.rows) || [];
    carousel.items = buildNotices(d);
    if (!carousel.items.length) { label.textContent = "本月"; setLogo("ok"); return; }

    // 当前各限额的级别（用于判定"进入区域"）
    const zones = {};   // name → level
    for (const r of rows) {
      if (r.used == null) continue;
      zones[r.name] = levelOf(r.used);
    }

    // 1) 恢复：限额从非 ok 回到 ok → 绿 + "已重置" 提示 10s
    const prevZones = btnState.zones || {};
    const recovered = Object.keys(prevZones).filter(n =>
      prevZones[n] !== "ok" && (zones[n] || "ok") === "ok");
    if (recovered.length) {
      setLogo("recovered");
      playSeq([`${shortName(rows.find(r => r.name === recovered[0]))}已重置`], [10]);
      btnState.zones = zones;
      return;
    }

    // 2) 进入区域提示：只在新进入 warn/danger 的限额上触发一次（每条播 3 次）
    btnState.zones = btnState.zones || {};
    const entering = Object.keys(zones).filter(n =>
      (zones[n] === "warn" || zones[n] === "danger") && btnState.zones[n] !== zones[n]);
    if (entering.length) {
      const r = rows.find(x => x.name === entering[0]);
      const name = shortName(r);
      const lv = zones[entering[0]];
      if (!r) { btnState.zones = zones; return; }
      const rm = resetIn(r.resetTime);
      const remain = (100 - r.used).toFixed(1);
      // 进入区域提示：一条消息播 3 遍；每段时长按自身长度（playSeq 内计算）
      const warnText = `${name}仅剩 ${remain}% · 预计 ${rm} 后重置`;
      const dangerText = `${name}仅剩 ${remain}% · 谨慎使用`;
      const msg = lv === "warn" ? warnText : dangerText;
      setLogo(lv);
      blinkLogo(lv, lv === "danger" ? 5 : 3, lv === "danger" ? 560 : 1300);
      playSeq(Array(3).fill(msg));
      btnState.zones = zones;
      return;
    }
    btnState.zones = zones;

    // 3) 正常轮播；Logo 颜色由 startCarousel 按当前轮播项的限额状态设置，这里不干预
    if (!carousel.running) { setLogo("ok"); startCarousel(); }
  }

  function setOffline() {
    alive = false;
    label.textContent = "看板未启动";
    setLogo("ok");
    icon.style.color = "var(--kb-faint)";
  }

  async function refresh() {
    try {
      const d = await fetchStats();
      renderBtnNotify(d);
      btn.title = `本账期起算 ${(d.cost && d.cost.cycleStart) ? new Date(d.cost.cycleStart).toLocaleString("zh-CN", {hour12: false}) : ""}\n已用 ${fmtTok(d.cards && d.cards.month && d.cards.month.total)} tokens\n含 cc-switch 合并的 KimiCode 用量`;
      if (panel.style.display === "block") renderPanel(d);
    } catch (e) {
      // 诊断：显示具体失败原因（权限/CORS/网络），定位后恢复"看板未启动"
      label.textContent = "看板未启动: " + (e && e.message ? e.message : String(e)).slice(0, 40);
      btn.title = (e && e.stack ? e.stack : String(e)).slice(0, 500);
      setLogo("ok");
      icon.style.color = "var(--kb-faint)";
    }
  }

  // ---- 右侧面板：三个圆形进度条 + 底部用量/费用 ----
  const panel = document.createElement("div");
  panel.id = "kimi-board-panel";
  Object.assign(panel.style, {
    position: "fixed", top: "14px", right: "12px", zIndex: "2147483646",
    width: "128px", background: "var(--kb-bg)", border: "1px solid var(--kb-line)",
    borderRadius: "14px", boxShadow: "0 10px 34px rgba(15,25,50,.14)",
    fontFamily: FONT, color: "var(--kb-text)", padding: "12px 10px 10px",
    display: "none",
  });

  function ringHTML(name, usedPct, extra, colorVar) {
    const r = 26, c = 2 * Math.PI * r, dash = (Math.max(0, Math.min(100, usedPct || 0)) / 100) * c;
    const warn = (usedPct || 0) >= 95 ? "var(--kb-red)" : (usedPct || 0) >= 80 ? "var(--kb-orange)" : colorVar;
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:3px">
      <div style="position:relative;width:60px;height:60px">
        <svg width="60" height="60" viewBox="0 0 60 60">
          <circle cx="30" cy="30" r="${r}" fill="none" stroke="var(--kb-soft)" stroke-width="5"/>
          <circle cx="30" cy="30" r="${r}" fill="none" stroke="${warn}" stroke-width="5"
            stroke-linecap="round" stroke-dasharray="${dash} ${c}"
            transform="rotate(-90 30 30)"/>
        </svg>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center">
          <span style="font-family:${MONO};font-size:11px;font-weight:700;color:var(--kb-text)">${fmtPct(usedPct)}</span>
        </div>
      </div>
      <span style="font-size:10px;color:var(--kb-dim);white-space:nowrap">${name}</span>
      <span style="font-size:9px;color:var(--kb-faint);white-space:nowrap">${extra || ""}</span>
    </div>`;
  }

  panel.innerHTML = `
    <div id="kbRings" style="display:flex;flex-direction:column;align-items:center;gap:13px"></div>
    <div style="margin-top:11px;border-top:1px solid var(--kb-line);padding-top:9px;display:flex;flex-direction:column;gap:5px;font-size:11px;color:var(--kb-dim)" id="kbStats"></div>
    <div style="margin-top:8px;text-align:center;font-size:9px;color:var(--kb-faint)" id="kbFoot"></div>`;
  const kbRings = panel.querySelector("#kbRings");
  const kbStats = panel.querySelector("#kbStats");
  const kbFoot = panel.querySelector("#kbFoot");

  function renderPanel(d) {
    const rows = (d.limits && d.limits.rows) || [];
    const find = (kw) => rows.find(r => r.name.includes(kw));
    const month = find("月额度") || find("月");
    const h5 = find("5") || find("小时");
    const wk = find("周");
    const order = [["月额度", month, "var(--kb-bar1)"],
                   ["5 小时", h5, "var(--kb-bar2)"],
                   ["周限额", wk, "var(--kb-bar3)"]];
    kbRings.innerHTML = order.map(([label, r, cv]) => {
      if (!r || r.used == null) return ringHTML(label, 0, "暂无数据", cv);
      const reset = r.resetTime ? new Date(r.resetTime).toLocaleString("zh-CN", {hour12: false}).slice(5, 16) : "";
      return ringHTML(label, r.used, reset ? reset + " 重置" : "", cv);
    }).join("");

    const c = d.cost || {};
    const cards = d.cards || {};
    const mt = cards.month && cards.month.total;
    kbStats.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span>本账期</span><span style="font-family:${MONO};color:var(--kb-text);font-weight:700">${mt ? fmtCN(mt) : "--"} tokens</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span>等效 API</span><span style="font-family:${MONO};color:var(--kb-text);font-weight:700">${yuan(c.monthTotal)}</span>
      </div>`;
    kbFoot.textContent = c.cycleLabel ? "周期 " + c.cycleLabel + " 起算" : "";
  }

  btn.onclick = async () => {
    if (!alive) await refresh();
    if (alive) window.open(DASH, "_blank");
    else toast("看板服务未运行：请先双击解压目录里的「start.bat」（或已配置开机自启则稍等片刻）");
  };

  const toast = (msg) => {
    const t = document.createElement("div");
    t.textContent = msg;
    Object.assign(t.style, {
      position: "fixed", left: "18px", bottom: "70px", zIndex: "2147483647",
      padding: "10px 14px", borderRadius: "10px", background: "rgba(255,255,255,.97)",
      border: "1px solid #e2e6ee", boxShadow: "0 4px 16px rgba(20,30,50,.10)",
      color: "#3a4356", fontSize: "12.5px", lineHeight: "1.6", fontFamily: FONT, maxWidth: "250px",
    });
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  };

  function mount() {
    const footer = document.querySelector("aside.side .col > .side-footer");
    if (!footer) return false;
    if (!document.getElementById("kimi-board-btn")) footer.before(btn);
    if (!document.getElementById("kimi-board-panel")) document.body.appendChild(panel);
    return true;
  }
  if (!mount()) {
    const retry = setInterval(() => { if (mount()) clearInterval(retry); }, 800);
    setTimeout(() => clearInterval(retry), 30000);
  }
  new MutationObserver(() => {
    if (!document.getElementById("kimi-board-btn")) mount();
  }).observe(document.body, { childList: true, subtree: true });

  applyTheme();
  panel.style.display = "block";  // 右侧面板默认显示（三个限额圆环）
  refresh();
  setInterval(refresh, 30000);
  new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme"] });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  }

  // 通知后台：可尝试同步官网月额度（扩展用户已登录 kimi.com 时）
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
    try { chrome.runtime.sendMessage({ type: "kb-sync-subscription" }, () => {}); } catch (e) {}
  }
})();
