// Kimi Board 月额度同步（可选）：用浏览器里 www.kimi.com 的登录态
// 请求 GetSubscriptionStats，把解析后的数据推给本机看板。
// 凭据（cookie/token）始终留在浏览器内，不经过看板；只传额度结果。
(() => {
  const DASH = "http://127.0.0.1:8321";
  const STATS = "https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats";
  const COOKIE_NAME = "kimi-auth";
  let secret = null;

  async function getSecret() {
    // 看板每次启动会换随机 secret；host_permissions 允许跨源读取页面
    const res = await fetch(DASH + "/", { cache: "no-store" });
    const html = await res.text();
    const m = html.match(/<meta name="kb-secret" content="([^"]+)">/);
    return m ? m[1] : null;
  }

  async function ensureSecret() {
    if (secret) return secret;
    secret = await getSecret();
    return secret;
  }

  // 看板配置的数据源是否接受扩展推送（auto 或 extension 才推，webview/manual 不打扰）
  async function boardWantsExtension() {
    try {
      const res = await fetch(DASH + "/api/settings", { cache: "no-store" });
      if (!res.ok) return false;
      const st = await res.json();
      const src = st.config && st.config.subscription && st.config.subscription.source;
      return src === "auto" || src === "extension";
    } catch {
      return false; // 看板未启动
    }
  }

  function postToBoard(text) {
    return fetch(DASH + "/api/subscription?source=extension", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KB-Secret": secret || "" },
      body: text,
    }).then(r => r.status);
  }

  async function fetchWithCreds() {
    // 优先直接用扩展的跨域请求携带 www.kimi.com cookie
    const res = await fetch(STATS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "include",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.text();
  }

  async function sync() {
    try {
      // 看板配置不接受扩展推送时（webview/manual），直接跳过，避免无脑打扰
      if (!(await boardWantsExtension())) return;
      let text;
      try {
        text = await fetchWithCreds();
      } catch (e) {
        // 兜底：从 cookie 里取 kimi-auth JWT，显式带 Authorization 头
        const cookie = await chrome.cookies.get({ url: "https://www.kimi.com", name: COOKIE_NAME });
        if (!cookie) throw e;
        const res = await fetch(STATS, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Authorization": "Bearer " + cookie.value },
          body: "{}",
        });
        if (!res.ok) throw e;
        text = await res.text();
      }
      // 校验确实是 GetSubscriptionStats 的返回
      const obj = JSON.parse(text);
      if (!obj.subscriptionBalance && !obj.ratelimitCode5h) return;
      if (!(await ensureSecret())) return;
      const status = await postToBoard(text);
      if (status === 403) { // 看板可能已重启，secret 变了，重取再试一次
        secret = null;
        if (await ensureSecret()) await postToBoard(text);
      }
    } catch (err) {
      /* 未登录 www.kimi.com 或看板未启动，静默 */
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "kb-sync-subscription") {
      sync().then(() => sendResponse({ ok: true }));
      return true; // 异步响应
    }
  });

  // 页面打开时主动同步一次
  sync();
  // 每 10 分钟重试一次（Service Worker 存活期间）
  setInterval(sync, 10 * 60 * 1000);
})();
