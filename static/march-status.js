(() => {
  "use strict";
  const stage = document.querySelector(".scene-stage");
  const chat = document.getElementById("chat");
  if (!stage || !chat || document.getElementById("march-status")) return;

  const labels = {
    idle: ["待机中", "输入消息，开始聊天"],
    thinking: ["深度思考中", "正在思考你的问题"],
    searching: ["正在搜索", "正在检索资料"],
    working: ["正在执行", "正在处理这次任务"],
    waiting: ["正在等待", "正在连接，等待响应"],
    done: ["回复完成", "这次任务已经结束"],
    error: ["请求失败", "请查看消息中的错误提示"],
    paused: ["已停止", "这次回复已取消"],
  };
  const component = document.createElement("div");
  component.id = "march-status";
  component.className = "march-status";
  component.setAttribute("role", "status");
  component.setAttribute("aria-live", "polite");
  component.setAttribute("aria-atomic", "true");
  const figure = document.createElement("img");
  figure.className = "march-status-figure";
  figure.alt = "";
  figure.setAttribute("aria-hidden", "true");
  figure.width = 72;
  figure.height = 72;
  figure.decoding = "async";
  const caption = document.createElement("div");
  caption.className = "march-status-caption";
  const title = document.createElement("strong");
  title.className = "march-status-title";
  const detail = document.createElement("span");
  detail.className = "march-status-detail";
  caption.append(title, detail);
  component.append(figure, caption);
  stage.insertBefore(component, chat);
  stage.classList.add("march-status-enabled");

  let state = "idle";
  let terminal = null;
  let turn = 0;
  let idleTimer = null;
  function requestIsBusy() { return typeof busy !== "undefined" && busy; }
  function paint(next, customTitle) {
    const label = customTitle || labels[next][0];
    if (component.dataset.state === next && title.textContent === label) return;
    if (component.dataset.state !== next) figure.src = "/static/ui/march-" + next + ".png";
    state = next;
    component.dataset.state = next;
    title.textContent = label;
    detail.textContent = labels[next][1];
  }
  function cancelIdleTimer() {
    if (idleTimer !== null) clearTimeout(idleTimer);
    idleTimer = null;
  }
  function returnToIdleLater() {
    if (!terminal || requestIsBusy() || idleTimer !== null) return;
    const completedTurn = turn;
    const thisTimer = setTimeout(() => {
      if (idleTimer !== thisTimer) return;
      idleTimer = null;
      if (completedTurn !== turn || requestIsBusy()) return;
      terminal = null;
      paint("idle");
    }, terminal === "error" ? 3200 : 2400);
    idleTimer = thisTimer;
  }
  function finish(next) {
    if (terminal && terminal !== next && next === "done") return;
    if (terminal !== next) cancelIdleTimer();
    terminal = next;
    paint(next);
    returnToIdleLater();
  }
  function beginTurn() {
    cancelIdleTimer();
    turn += 1;
    terminal = null;
    paint("waiting");
    return turn;
  }
  function activity(next, customTitle) {
    if (!terminal) paint(next, customTitle);
  }
  function observeStatus(text, kind) {
    const message = String(text || "");
    if (kind === "error") { finish("error"); return; }
    if (/^Response stopped\b/i.test(message)) { finish("paused"); return; }
    if (kind === "busy") {
      // The real send calls this once; it also covers handlers holding an older send reference.
      if (/^Starting(?:…|\.{3})$/i.test(message)) beginTurn();
      else if (/^Starting\b/i.test(message)) activity("waiting");
      else if (/^Thinking\b/i.test(message)) activity("thinking");
      else if (/^Streaming response\b/i.test(message)) activity("working", "正在写回复");
      else if (/^Finishing\b/i.test(message)) activity("working", "正在整理结果");
      else activity("working");
      return;
    }
    if (terminal) { returnToIdleLater(); return; }
    if (state !== "idle" && !requestIsBusy()) finish("done");
  }

  const originalSetStatus = window.setStatus;
  if (typeof originalSetStatus === "function") {
    window.setStatus = function (...args) {
      const result = originalSetStatus.apply(this, args);
      observeStatus(args[0], args[1] || "ready");
      return result;
    };
  }
  const originalStreamEvent = window.handleStreamEvent;
  if (typeof originalStreamEvent === "function") {
    window.handleStreamEvent = function (data, assistant, bodyEl) {
      const result = originalStreamEvent.apply(this, arguments);
      if (!data) return result;
      if (data.event === "error" || data.error || assistant?.status === "error") finish("error");
      else if (data.event === "done") finish("done");
      else if (data.event === "start") activity("waiting");
      else if (data.event === "delta" || data.delta) {
        if (data.delta || data.text) activity("working", "正在写回复");
      } else if (data.event === "thinking") activity("thinking");
      else if (data.event === "tool" || data.tool) {
        const tool = data.tool || data.name || "executing";
        const name = typeof tool === "string" ? tool : String(tool.name || "");
        activity(/搜索|search|web|browser/i.test(name) ? "searching" : "working");
      } else if (data.event === "result") activity("working", "正在整理结果");
      return result;
    };
  }
  const originalSend = window.send;
  if (typeof originalSend === "function") {
    window.send = function (...args) {
      const input = document.getElementById("input");
      if (!input?.value.trim() || requestIsBusy()) return originalSend.apply(this, args);
      const previousTurn = turn;
      let result;
      try { result = originalSend.apply(this, args); }
      catch (error) {
        if (turn === previousTurn) beginTurn();
        finish(error?.name === "AbortError" ? "paused" : "error");
        throw error;
      }
      if (turn === previousTurn) beginTurn();
      const thisTurn = turn;
      return Promise.resolve(result).then(value => {
        if (thisTurn === turn) {
          if (!terminal) finish("done");
          returnToIdleLater();
        }
        return value;
      }, error => {
        if (thisTurn === turn) finish(error?.name === "AbortError" ? "paused" : "error");
        throw error;
      });
    };
  }
  const originalStop = window.stop;
  if (typeof originalStop === "function") {
    window.stop = function (...args) {
      const controller = typeof activeController !== "undefined" ? activeController : null;
      const result = originalStop.apply(this, args);
      if (controller?.signal.aborted) finish("paused");
      return result;
    };
  }

  function decorateChat() {
    chat.querySelectorAll(".msg.assistant .avatar").forEach(avatar => {
      if (avatar.classList.contains("march-avatar")) return;
      const image = document.createElement("img");
      image.src = "/static/ui/march-avatar-v2.png?v=20261006-q2";
      image.alt = "";
      image.setAttribute("aria-hidden", "true");
      image.width = 36;
      image.height = 44;
      image.decoding = "async";
      avatar.replaceChildren(image);
      avatar.classList.add("march-avatar");
      avatar.setAttribute("role", "img");
      avatar.setAttribute("aria-label", "三月七助手头像");
    });
    chat.querySelectorAll(".empty h2").forEach(heading => { heading.textContent = "小三月的agent"; });
  }
  const originalRender = window.render;
  if (typeof originalRender === "function") {
    window.render = function (...args) {
      const result = originalRender.apply(this, args);
      decorateChat();
      if (!requestIsBusy()) returnToIdleLater();
      return result;
    };
  }
  paint("idle");
  decorateChat();
  const hint = document.getElementById("hint");
  if (requestIsBusy() && hint) observeStatus(hint.textContent, hint.dataset.state);
})();
