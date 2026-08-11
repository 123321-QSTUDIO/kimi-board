// 在 Kimi Code WebUI 左侧边栏底部（与 Kimi Monitor 同一区域、设置按钮上方）
// 注入"token 看板"小组件：实时显示本月已用 token 数（每 60s 自动刷新）;
// 点击打开看板；服务未运行时变灰，点击提示启动方法。
// WebUI 是 SPA,侧栏会重绘,用 MutationObserver 保证组件被移除后重新挂载。
(() => {
  const DASH = "http://127.0.0.1:8321";
  const FONT = '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
  const MONO = 'ui-monospace, Consolas, "Cascadia Mono", monospace';

  const btn = document.createElement("div");
  btn.id = "kimi-board-btn";
  Object.assign(btn.style, {
    display: "flex",
    alignItems: "baseline",
    gap: "7px",
    margin: "2px 12px 8px",
    padding: "8px 12px",
    borderRadius: "10px",
    background: "#fff",
    border: "1px solid #e8ebf0",
    fontFamily: FONT,
    cursor: "pointer",
    userSelect: "none",
    transition: "border-color .15s",
  });
  btn.onmouseenter = () => (btn.style.borderColor = "#2e6fe8");
  btn.onmouseleave = () => (btn.style.borderColor = "#e8ebf0");

  const icon = document.createElement("span");
  icon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="#2e6fe8" stroke-width="2.4" stroke-linejoin="round" aria-hidden="true"><polygon points="12,2.6 20.3,7.4 20.3,16.6 12,21.4 3.7,16.6 3.7,7.4"/></svg>';
  Object.assign(icon.style, { display: "inline-flex", alignItems: "center", lineHeight: "0", alignSelf: "center", flexShrink: "0" });

  const label = document.createElement("span");
  label.textContent = "本账期";
  Object.assign(label.style, { fontSize: "11px", color: "#9aa4b8", lineHeight: "1", whiteSpace: "nowrap", flexShrink: "0" });

  const cost = document.createElement("span");
  cost.textContent = "…";
  Object.assign(cost.style, {
    fontFamily: MONO, fontWeight: "700", fontSize: "12.5px", lineHeight: "1",
    fontVariantNumeric: "tabular-nums", color: "#1c2433", whiteSpace: "nowrap",
  });

  const price = document.createElement("span");
  price.textContent = "";
  Object.assign(price.style, {
    fontFamily: MONO, fontWeight: "500", fontSize: "10.5px", lineHeight: "1",
    color: "#8b96a8", whiteSpace: "nowrap", flexShrink: "0",
  });

  const arrow = document.createElement("span");
  arrow.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="#c3cad8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  Object.assign(arrow.style, { display: "inline-flex", alignItems: "center", lineHeight: "0", marginLeft: "auto", alignSelf: "center", flexShrink: "0" });

  btn.append(icon, label, cost, price, arrow);

  let alive = false;

  function setOffline() {
    alive = false;
    label.style.display = "none";
    price.textContent = "";
    cost.textContent = "看板未启动";
    Object.assign(cost.style, {
      fontFamily: FONT, fontWeight: "500", fontSize: "12px", color: "#9aa4b8",
    });
  }

  // 中文缩略：≥亿 用"亿"，≥万 用"万"（与看板一致），<1万 返回完整数字
  function fmtCN(n) {
    if (n >= 1e8) return (n / 1e8).toFixed(2).replace(/\.?0+$/, "") + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, "") + "万";
    return String(Math.round(n));
  }

  async function refresh() {
    try {
      const d = await (await fetch(DASH + "/api/stats", { cache: "no-store" })).json();
      alive = true;
      label.style.display = "";
      // 标签跟随看板计费周期（含 cc-switch 合并的量）：本账期 · 起算日 MM-DD
      const cs = d.cost || {};
      const cl = (cs.cycleLabel || "").slice(0, 5);  // "07-20 11:22" -> "07-20"
      label.textContent = "本账期" + (cl ? " " + cl : "");
      const t = d.cards.month.total;
      cost.textContent = fmtCN(t) + " tokens";
      price.textContent = (cs.monthTotal != null && cs.monthTotal > 0)
        ? "≈¥" + (cs.monthTotal >= 10000 ? (cs.monthTotal / 10000).toFixed(2).replace(/\.?0+$/, "") + "万" : cs.monthTotal.toFixed(0))
        : "";
      Object.assign(cost.style, {
        fontFamily: MONO, fontWeight: "700", fontSize: "12.5px", color: "#1c2433",
      });
      btn.title = `本账期起算 ${cs.cycleStart ? new Date(cs.cycleStart).toLocaleString("zh-CN", {hour12: false}) : ""}\n已用 ${fmtCN(t)} tokens · 等效费用 ≈¥${cs.monthTotal != null ? cs.monthTotal.toFixed(2) : "--"}\n含 cc-switch 合并的 KimiCode 用量`;
    } catch {
      setOffline();
    }
  }

  const toast = (msg) => {
    const t = document.createElement("div");
    t.textContent = msg;
    Object.assign(t.style, {
      position: "fixed",
      left: "18px",
      bottom: "70px",
      zIndex: "2147483647",
      padding: "10px 14px",
      borderRadius: "10px",
      background: "rgba(255,255,255,.97)",
      border: "1px solid #e2e6ee",
      boxShadow: "0 4px 16px rgba(20,30,50,.10)",
      color: "#3a4356",
      fontSize: "12.5px",
      lineHeight: "1.6",
      fontFamily: FONT,
      maxWidth: "250px",
    });
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  };

  btn.onclick = async () => {
    if (!alive) await refresh();
    if (alive) window.open(DASH, "_blank");
    else toast("看板服务未运行：请先双击解压目录里的「start.bat」（或已配置开机自启则稍等片刻）");
  };

  // 挂载到侧栏 .col 内、.side-footer 之前;SPA 重绘后自动重新挂载
  function mount() {
    const footer = document.querySelector("aside.side .col > .side-footer");
    if (!footer) return false;
    if (!document.getElementById("kimi-board-btn")) footer.before(btn);
    return true;
  }
  if (!mount()) {
    const retry = setInterval(() => { if (mount()) clearInterval(retry); }, 800);
    setTimeout(() => clearInterval(retry), 30000);
  }
  new MutationObserver(() => {
    if (!document.getElementById("kimi-board-btn")) mount();
  }).observe(document.body, { childList: true, subtree: true });

  refresh();
  setInterval(refresh, 60000);

  // 通知后台：可尝试同步官网月额度（扩展用户已登录 kimi.com 时）
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
    try { chrome.runtime.sendMessage({ type: "kb-sync-subscription" }, () => {}); } catch (e) {}
  }
})();
