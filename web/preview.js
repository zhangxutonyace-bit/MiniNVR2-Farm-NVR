/* Camera-only viewer: capabilities stay in memory, never query strings or storage. */
(() => {
  "use strict";
  const ticket = new URLSearchParams(location.hash.slice(1)).get("t") || "";
  history.replaceState(null, "", location.pathname);
  const image = document.getElementById("picture");
  const empty = document.getElementById("empty");
  const status = document.getElementById("status");
  const retry = document.getElementById("retry");
  const expiry = document.getElementById("expiry");
  const cameraTitle = document.getElementById("camera");
  let cameraId = "";
  let deadline = 0;
  let timer = 0;
  let controller = null;
  let currentUrl = "";
  let stopped = false;
  let busy = false;

  function clearPicture(message) {
    image.hidden = true;
    image.removeAttribute("src");
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    currentUrl = "";
    empty.textContent = message || "暂无当前画面";
    empty.hidden = false;
  }

  function show(message, error = false) {
    status.textContent = message;
    status.dataset.kind = error ? "error" : "";
  }

  function finish(message) {
    stopped = true;
    clearTimeout(timer);
    if (controller) controller.abort();
    clearPicture("预览已结束");
    expiry.textContent = "预览链接已失效 · 请回微信重新发送预览";
    show(message, true);
    retry.hidden = true;
  }

  function expired() {
    if (deadline && performance.now() >= deadline) {
      finish("链接已过期，请回微信重新发送预览。");
      return true;
    }
    return false;
  }

  async function request(path, kind = "json") {
    controller = new AbortController();
    const active = controller;
    const timeout = setTimeout(() => active.abort(), 8000);
    try {
      const response = await fetch(path, {
        method: "GET", headers: {Authorization: "Bearer " + ticket},
        credentials: "omit", cache: "no-store", redirect: "error", signal: active.signal
      });
      let data = null;
      if (response.ok && kind === "blob") {
        if (!(response.headers.get("Content-Type") || "").startsWith("image/jpeg")) {
          throw new Error("unexpected response");
        }
        data = await response.blob();
      } else {
        try { data = await response.json(); } catch (_) { /* A shutdown may return no JSON. */ }
      }
      return {ok: response.ok, status: response.status, data};
    } finally {
      clearTimeout(timeout);
      if (controller === active) controller = null;
    }
  }

  function failure(response) {
    let message = "无法获取当前画面，请检查电脑与手机的 Wi-Fi 连接。";
    if (response.data && typeof response.data.msg === "string") message = response.data.msg;
    if ([401, 403, 410].includes(response.status)) {
      finish(message);
      return true;
    }
    clearPicture("当前画面不可用");
    show(message, true);
    retry.hidden = false;
    return false;
  }

  function schedule(delay = 1000) {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(refresh, delay);
  }

  async function refresh() {
    if (stopped || busy || document.hidden || expired()) return;
    busy = true;
    retry.disabled = true;
    try {
      if (!cameraId) {
        const info = await request("/info");
        if (!info.ok) {
          failure(info);
          return;
        }
        const data = info.data || {};
        if (stopped || document.hidden) return;
        if (typeof data.camera_id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(data.camera_id)
            || !Number.isFinite(data.expires_in) || data.expires_in <= 0) {
          finish("链接已失效，请回微信重新发送预览。");
          return;
        }
        cameraId = data.camera_id;
        deadline = performance.now() + data.expires_in * 1000;
        cameraTitle.textContent = String(data.name || "摄像头预览");
      }
      if (expired()) return;
      const response = await request("/frame/" + encodeURIComponent(cameraId), "blob");
      if (stopped || document.hidden || expired()) return;
      if (!response.ok) {
        failure(response);
        return;
      }
      const blob = response.data;
      if (stopped || document.hidden || expired()) return;
      const next = URL.createObjectURL(blob);
      const previous = currentUrl;
      currentUrl = next;
      image.src = next;
      image.hidden = false;
      empty.hidden = true;
      if (previous) URL.revokeObjectURL(previous);
      show("当前画面 · 每秒刷新");
      retry.hidden = true;
    } catch (_) {
      if (!stopped && !document.hidden) {
        clearPicture("连接已中断");
        show("无法连接电脑。请确认处于同一 Wi-Fi、电脑预览仍开启，且未被防火墙拦截。", true);
        retry.hidden = false;
      }
    } finally {
      busy = false;
      retry.disabled = false;
      schedule();
    }
  }

  retry.addEventListener("click", () => {
    if (!stopped && !busy) {
      show("正在重新连接…");
      schedule(0);
    }
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (document.hidden) {
      if (controller) controller.abort();
      clearPicture("已暂停预览");
      show("返回此页面后恢复当前画面。");
    } else if (!stopped && !expired()) {
      schedule(0);
    }
  });
  window.addEventListener("pagehide", () => {
    if (controller) controller.abort();
    clearPicture("已暂停预览");
    clearTimeout(timer);
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted && !stopped && !expired()) schedule(0);
  });
  setInterval(() => {
    if (stopped || expired() || !deadline) return;
    const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
    expiry.textContent = "链接剩余 " + Math.floor(seconds / 60) + " 分 " + (seconds % 60) + " 秒 · 同一 Wi-Fi 可查看";
  }, 1000);

  if (!/^[A-Za-z0-9_-]{43}$/.test(ticket)) {
    finish("链接无效，请回微信重新发送预览，并打开新链接。");
  } else {
    schedule(0);
  }
})();
