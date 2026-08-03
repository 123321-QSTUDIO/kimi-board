// 在 Kimi Code WebUI 页面注入"token 看板"悬浮按钮。
// 点击前探测看板服务（127.0.0.1:8321）是否在运行：在则新标签页打开，不在则提示如何启动。
(() => {
  if (document.getElementById("kimi-board-btn")) return;

  const DASH = "http://127.0.0.1:8321";

  const btn = document.createElement("div");
  btn.id = "kimi-board-btn";
  btn.textContent = "⬡ token 看板";
  Object.assign(btn.style, {
    position: "fixed",
    right: "18px",
    bottom: "18px",
    zIndex: "2147483647",
    padding: "8px 16px",
    borderRadius: "999px",
    background: "#3a8dff",
    color: "#fff",
    fontSize: "13px",
    fontWeight: "600",
    fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    cursor: "pointer",
    boxShadow: "0 4px 14px rgba(46,111,232,.35)",
    userSelect: "none",
    transition: "background .15s, transform .15s",
  });
  btn.onmouseenter = () => (btn.style.transform = "translateY(-1px)");
  btn.onmouseleave = () => (btn.style.transform = "");

  const toast = (msg) => {
    const t = document.createElement("div");
    t.textContent = msg;
    Object.assign(t.style, {
      position: "fixed",
      right: "18px",
      bottom: "64px",
      zIndex: "2147483647",
      padding: "10px 14px",
      borderRadius: "10px",
      background: "#101828",
      color: "#fff",
      fontSize: "12.5px",
      lineHeight: "1.6",
      fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      maxWidth: "260px",
    });
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  };

  btn.onclick = async () => {
    btn.style.background = "#2e6fe8";
    try {
      await fetch(DASH + "/api/stats", { cache: "no-store" });
      window.open(DASH, "_blank");
    } catch {
      toast("看板服务未运行：请先双击解压目录里的「start.bat」（或已配置开机自启则稍等片刻）");
    } finally {
      btn.style.background = "#3a8dff";
    }
  };

  document.body.appendChild(btn);
})();
