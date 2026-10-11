(() => {
  "use strict";
  if (typeof renderConvList !== "function" || typeof apiFetch !== "function" || renderConvList.conversationActionsWrapped) return;
  const list = document.getElementById("conv-list");
  if (!list) return;
  const originalRender = renderConvList;
  const pending = new Set();
  let menuCID = null, menuTrigger = null, renameCID = null, renameTrigger = null;
  let priorAppInert = false, noticeTimer = null;

  const menu = document.createElement("div");
  menu.id = "conversation-actions-menu";
  menu.className = "conv-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "会话操作");
  menu.innerHTML = '<p class="conv-menu-heading"></p><button type="button" class="conv-menu-pin" role="menuitem"></button><button type="button" class="conv-menu-rename" role="menuitem">修改标题</button><p class="conv-menu-error" role="alert" hidden></p>';
  document.body.appendChild(menu);
  const pinButton = menu.querySelector(".conv-menu-pin");
  const renameButton = menu.querySelector(".conv-menu-rename");
  const menuError = menu.querySelector(".conv-menu-error");

  const notice = document.createElement("div");
  notice.className = "conv-operation-notice";
  notice.hidden = true;
  notice.innerHTML = '<span class="conv-notice-message" role="status" aria-live="polite"></span><button type="button" aria-label="关闭提示">×</button>';
  document.body.appendChild(notice);
  notice.querySelector("button").onclick = () => { notice.hidden = true; clearTimeout(noticeTimer); };

  const renameLayer = document.createElement("div");
  renameLayer.id = "conversation-title-dialog";
  renameLayer.hidden = true;
  renameLayer.innerHTML = '<section class="conv-title-card" role="dialog" aria-modal="true" aria-labelledby="conv-title-heading" aria-describedby="conv-title-help"><form novalidate><div class="conv-title-head"><h2 id="conv-title-heading">修改会话标题</h2><button type="button" class="conv-title-close" aria-label="关闭修改标题">×</button></div><div class="conv-title-body"><p class="conv-title-context"></p><label for="conv-title-input">会话标题</label><input id="conv-title-input" type="text" maxlength="80" autocomplete="off" required aria-describedby="conv-title-help conv-title-error"><p id="conv-title-help">最多 80 个字符，修改不会中断正在生成的回复。</p><p id="conv-title-error" role="alert" hidden></p></div><div class="conv-title-footer"><button type="button" class="conv-title-cancel">取消</button><button type="submit" class="conv-title-save">保存标题</button></div></form></section>';
  document.body.appendChild(renameLayer);
  const app = document.getElementById("app");
  const titleForm = renameLayer.querySelector("form");
  const titleInput = renameLayer.querySelector("input");
  const titleError = renameLayer.querySelector("#conv-title-error");
  const saveButton = renameLayer.querySelector(".conv-title-save");

  function findEntry(cid) { return convList.find(entry => entry.conversation_id === cid); }
  function findMore(cid) {
    return [...list.querySelectorAll(".conv-item[data-cid]")].find(item => item.dataset.cid === cid)?.querySelector(".conv-more") || null;
  }
  function restoreFocus(cid, fallback) {
    const target = findMore(cid) || (fallback?.isConnected ? fallback : null) || document.getElementById("menu-btn");
    target?.focus({ preventScroll: true });
  }
  function notify(message, error = false) {
    clearTimeout(noticeTimer);
    const content = notice.querySelector(".conv-notice-message");
    content.setAttribute("role", error ? "alert" : "status");
    content.setAttribute("aria-live", error ? "assertive" : "polite");
    content.textContent = message;
    notice.dataset.kind = error ? "error" : "success";
    notice.hidden = false;
    if (!error) noticeTimer = setTimeout(() => { notice.hidden = true; }, 5000);
  }
  function timestamp(value) {
    if (value === null || value === undefined || value === "") return 0;
    const number = Number(value);
    if (Number.isFinite(number)) return number < 100000000000 ? number * 1000 : number;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  function positionMenu() {
    if (menu.hidden || !menuTrigger?.isConnected) return;
    const rect = menuTrigger.getBoundingClientRect();
    const viewport = window.visualViewport;
    const topEdge = viewport?.offsetTop || 0;
    const leftEdge = viewport?.offsetLeft || 0;
    const width = viewport?.width || window.innerWidth;
    const height = viewport?.height || window.innerHeight;
    const left = Math.max(leftEdge + 8, Math.min(rect.right - menu.offsetWidth, leftEdge + width - menu.offsetWidth - 8));
    const below = rect.bottom + 6;
    const top = below + menu.offsetHeight <= topEdge + height - 8 ? below : Math.max(topEdge + 8, rect.top - menu.offsetHeight - 6);
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
  }
  function syncMenu() {
    const entry = findEntry(menuCID);
    if (!entry || !menuTrigger?.isConnected) { closeMenu(false); return; }
    menu.querySelector(".conv-menu-heading").textContent = entry.title || "未命名会话";
    pinButton.textContent = entry.pinned === true ? "取消置顶" : "置顶会话";
    pinButton.disabled = renameButton.disabled = pending.has(menuCID);
    menuTrigger.setAttribute("aria-expanded", "true");
    positionMenu();
  }
  function closeMenu(returnFocus = false) {
    const cid = menuCID, trigger = menuTrigger;
    menu.hidden = true;
    menuTrigger?.setAttribute("aria-expanded", "false");
    menuCID = menuTrigger = null;
    if (returnFocus) restoreFocus(cid, trigger);
  }
  function openMenu(cid, trigger, last = false) {
    if (menuCID === cid && !menu.hidden) { closeMenu(true); return; }
    closeMenu(false);
    menuCID = cid;
    menuTrigger = trigger;
    menuError.hidden = true;
    menuError.textContent = "";
    menu.hidden = false;
    syncMenu();
    (last ? renameButton : pinButton).focus();
  }
  function decorateList() {
    list.querySelectorAll(".conv-item[data-cid]").forEach(item => {
      const cid = item.dataset.cid;
      const entry = findEntry(cid);
      if (!entry) return;
      item.dataset.conversationActions = "on";
      if (entry.pinned === true) {
        const badge = document.createElement("span");
        badge.className = "conv-pin-badge";
        badge.textContent = "置顶";
        item.insertBefore(badge, item.querySelector(".conv-title"));
      }
      const more = document.createElement("button");
      more.type = "button";
      more.className = "conv-more";
      more.textContent = "⋯";
      more.setAttribute("aria-label", "会话更多操作");
      more.setAttribute("aria-haspopup", "menu");
      more.setAttribute("aria-controls", menu.id);
      more.setAttribute("aria-expanded", "false");
      more.disabled = pending.has(cid);
      more.addEventListener("click", event => { event.stopPropagation(); openMenu(cid, more); });
      more.addEventListener("keydown", event => {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault(); event.stopPropagation(); openMenu(cid, more, event.key === "ArrowUp");
      });
      item.insertBefore(more, item.querySelector(".conv-del"));
    });
    if (!menu.hidden) { menuTrigger = findMore(menuCID); syncMenu(); }
  }
  renderConvList = function (...args) {
    const focused = document.activeElement;
    const focusCID = focused?.closest?.(".conv-item[data-cid]")?.dataset.cid;
    const focusWasMore = focused?.classList?.contains("conv-more");
    convList = [...convList].sort((a, b) => Number(b.pinned === true) - Number(a.pinned === true) || timestamp(b.updated_at) - timestamp(a.updated_at));
    const result = originalRender.apply(this, args);
    decorateList();
    if (focusWasMore && renameLayer.hidden && menu.hidden) restoreFocus(focusCID, focused);
    return result;
  };
  renderConvList.conversationActionsWrapped = true;

  async function patchConversation(cid, suffix, body) {
    const response = await apiFetch("/api/conversations/" + encodeURIComponent(cid) + suffix + "?user_id=" + encodeURIComponent(userId), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    const data = payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
    if (!response.ok) {
      const fallback = response.status === 403 ? "没有权限修改这个会话。" : response.status === 404 ? "会话已不存在，请刷新列表。" : response.status === 422 ? "内容不符合要求，请检查后重试。" : "保存失败（" + response.status + "），请重试。";
      throw new Error(typeof data.detail === "string" ? data.detail : fallback);
    }
    const returnedCID = data.conversation_id || data.cid;
    if (returnedCID && returnedCID !== cid) throw new Error("服务器返回的会话编号不一致，请刷新列表后重试。");
    return data;
  }
  function errorMessage(error) {
    if (error?.message === "unauthorized") return "登录已过期，请重新登录。";
    if (error instanceof TypeError) return "连接失败，请检查网络后重试。";
    return error?.message || "保存失败，请重试。";
  }
  function markPending(cid, on) {
    if (on) pending.add(cid); else pending.delete(cid);
    const more = findMore(cid);
    if (more) more.disabled = on;
    if (menuCID === cid && !menu.hidden) pinButton.disabled = renameButton.disabled = on;
  }
  pinButton.onclick = async () => {
    const cid = menuCID, entry = findEntry(cid);
    if (!entry || pending.has(cid)) return;
    const desiredPinned = entry.pinned !== true;
    menuError.hidden = true;
    markPending(cid, true);
    try {
      const data = await patchConversation(cid, "/pin", { pinned: desiredPinned });
      if (typeof data.pinned !== "boolean") throw new Error("服务器未返回置顶状态，请刷新列表后重试。");
      const currentEntry = findEntry(cid);
      if (currentEntry) {
        currentEntry.pinned = data.pinned;
        if (data.updated_at !== undefined) currentEntry.updated_at = data.updated_at;
      }
      markPending(cid, false);
      renderConvList();
      if (menuCID === cid) closeMenu(true);
      notify(data.pinned ? "会话已置顶" : "已取消置顶");
    } catch (error) {
      const message = "置顶操作失败：" + errorMessage(error);
      if (menuCID === cid && !menu.hidden) { menuError.textContent = message; menuError.hidden = false; positionMenu(); }
      notify(message, true);
    } finally {
      markPending(cid, false);
      if (menuCID === cid && !menu.hidden && document.activeElement === document.body) pinButton.focus();
    }
  };

  function openRename(cid, trigger) {
    const entry = findEntry(cid);
    if (!entry || pending.has(cid)) return;
    closeMenu(false);
    renameCID = cid;
    renameTrigger = trigger;
    renameLayer.querySelector(".conv-title-context").textContent = "当前标题：" + (entry.title || "未命名会话");
    titleInput.value = entry.title || "";
    titleError.textContent = "";
    titleError.hidden = true;
    titleInput.removeAttribute("aria-invalid");
    titleInput.disabled = saveButton.disabled = false;
    saveButton.textContent = "保存标题";
    renameLayer.hidden = false;
    priorAppInert = !!app?.inert;
    if (app) app.inert = true;
    titleInput.focus({ preventScroll: true });
    titleInput.select();
  }
  function closeRename(returnFocus = true) {
    if (renameLayer.hidden) return;
    const cid = renameCID, trigger = renameTrigger;
    renameLayer.hidden = true;
    if (app) app.inert = priorAppInert;
    renameCID = renameTrigger = null;
    if (returnFocus) restoreFocus(cid, trigger);
  }
  renameButton.onclick = () => openRename(menuCID, menuTrigger);
  renameLayer.querySelector(".conv-title-close").onclick = () => closeRename();
  renameLayer.querySelector(".conv-title-cancel").onclick = () => closeRename();
  renameLayer.addEventListener("click", event => { if (event.target === renameLayer) closeRename(); });
  titleInput.addEventListener("input", () => { titleInput.removeAttribute("aria-invalid"); titleError.hidden = true; });
  titleForm.addEventListener("submit", async event => {
    event.preventDefault();
    const cid = renameCID;
    if (!cid || pending.has(cid)) return;
    const title = titleInput.value.trim();
    if (!title || title.length > 80) {
      titleError.textContent = !title ? "标题不能为空。" : "标题最多 80 个字符。";
      titleError.hidden = false;
      titleInput.setAttribute("aria-invalid", "true");
      titleInput.focus();
      return;
    }
    markPending(cid, true);
    titleError.hidden = true;
    titleInput.disabled = saveButton.disabled = true;
    saveButton.textContent = "正在保存…";
    try {
      const data = await patchConversation(cid, "", { title });
      const entry = findEntry(cid);
      if (entry) {
        entry.title = typeof data.title === "string" ? data.title : title;
        if (typeof data.pinned === "boolean") entry.pinned = data.pinned;
        if (data.updated_at !== undefined) entry.updated_at = data.updated_at;
      }
      markPending(cid, false);
      renderConvList();
      if (renameCID === cid) closeRename();
      notify("会话标题已更新");
    } catch (error) {
      const message = "修改标题失败：" + errorMessage(error);
      if (renameCID === cid && !renameLayer.hidden) { titleError.textContent = message; titleError.hidden = false; }
      else notify(message, true);
    } finally {
      markPending(cid, false);
      if (renameCID === cid && !renameLayer.hidden) {
        titleInput.disabled = saveButton.disabled = false;
        saveButton.textContent = "保存标题";
        if (document.activeElement === document.body) titleInput.focus();
      }
    }
  });
  renameLayer.addEventListener("keydown", event => {
    if (event.isComposing && event.key === "Enter") { event.preventDefault(); return; }
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeRename(); return; }
    if (event.key !== "Tab") return;
    const controls = [...renameLayer.querySelectorAll("button,input")].filter(element => !element.disabled && element.offsetParent !== null);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  menu.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeMenu(true); return; }
    if (event.key === "Tab") { closeMenu(true); return; }
    const controls = [...menu.querySelectorAll("button")].filter(button => !button.disabled);
    if (!controls.length || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = controls.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? controls.length - 1 : (index + (event.key === "ArrowUp" ? -1 : 1) + controls.length) % controls.length;
    controls[next].focus();
  });
  document.addEventListener("pointerdown", event => {
    if (!menu.hidden && !menu.contains(event.target) && !menuTrigger?.contains(event.target)) closeMenu(false);
  });
  list.addEventListener("scroll", () => closeMenu(false), { passive: true });
  window.addEventListener("resize", () => { if (!menu.hidden) positionMenu(); }, { passive: true });
  window.visualViewport?.addEventListener("resize", () => { if (!menu.hidden) positionMenu(); }, { passive: true });
  renderConvList();
})();
