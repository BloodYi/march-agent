(() => {
  "use strict";
  const storageKey = "agy_ui_preferences_v2";
  const legacyStorageKey = "agy_ui_preferences_v1";
  const sceneShadeDefaults = { immersive: 20, original: 65 };
  const defaults = { scene: "immersive", palette: "duet", characters: true, blur: 0, shade: sceneShadeDefaults.immersive };
  const palettes = ["duet", "firefly", "castorice"];
  const scenes = ["immersive", "original"];
  function readStoredObject(key) {
    try {
      const raw = JSON.parse(localStorage.getItem(key) || "null");
      return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
    } catch { return null; }
  }
  const storedV2 = readStoredObject(storageKey);
  const firstV2Activation = storedV2 === null;
  const needsSceneShadeMigration = !storedV2?.sceneShades || typeof storedV2.sceneShades !== "object" || Array.isArray(storedV2.sceneShades);
  function readShade(value, fallback) {
    return Number.isFinite(value) ? Math.max(0, Math.min(75, value)) : fallback;
  }
  function readPreferences() {
    const legacy = readStoredObject(legacyStorageKey) || {};
    const raw = storedV2 || legacy;
    const currentScene = scenes.includes(storedV2?.scene) ? storedV2.scene : defaults.scene;
    const savedShades = needsSceneShadeMigration ? {} : storedV2.sceneShades;
    const sceneShades = {
      immersive: readShade(savedShades.immersive, storedV2 && currentScene === "immersive" ? readShade(raw.shade, sceneShadeDefaults.immersive) : sceneShadeDefaults.immersive),
      original: readShade(savedShades.original, readShade(legacy.shade, sceneShadeDefaults.original)),
    };
    return {
      scene: currentScene,
      palette: palettes.includes(raw.palette) ? raw.palette : defaults.palette,
      characters: typeof raw.characters === "boolean" ? raw.characters : defaults.characters,
      blur: !firstV2Activation && Number.isFinite(raw.blur) ? Math.max(0, Math.min(8, raw.blur)) : defaults.blur,
      sceneShades,
      shade: sceneShades[currentScene],
    };
  }
  let preferences = readPreferences();
  const triggers = ["bg-toggle", "sidebar-ui-settings"].map(id => document.getElementById(id)).filter(Boolean);
  const trigger = triggers[0];
  if (!trigger) return;
  const layer = document.createElement("div");
  layer.id = "ui-settings";
  layer.hidden = true;
  layer.innerHTML =
    '<section class="ui-settings-card" role="dialog" aria-modal="true" aria-labelledby="ui-settings-title" aria-describedby="ui-settings-desc">' +
      '<div class="ui-settings-head"><div><h2 id="ui-settings-title">界面设置</h2><p id="ui-settings-desc">让聊天空间，成为你喜欢的样子</p></div>' +
      '<button class="ui-settings-close" type="button" aria-label="关闭界面设置">×</button></div>' +
      '<div class="ui-settings-body">' +
        '<fieldset class="ui-settings-section"><legend>主题配色</legend><div class="ui-palette-options">' +
          '<button type="button" class="ui-palette-option" data-palette="duet" aria-pressed="false"><span class="ui-palette-swatch ui-swatch-duet" aria-hidden="true"></span>流萤 × 遐蝶</button>' +
          '<button type="button" class="ui-palette-option" data-palette="firefly" aria-pressed="false"><span class="ui-palette-swatch ui-swatch-firefly" aria-hidden="true"></span>流萤 · 青绿</button>' +
          '<button type="button" class="ui-palette-option" data-palette="castorice" aria-pressed="false"><span class="ui-palette-swatch ui-swatch-castorice" aria-hidden="true"></span>遐蝶 · 蝶紫</button>' +
        '</div></fieldset>' +
        '<div class="ui-settings-section">' +
          '<label class="ui-settings-row" for="ui-scene"><span>界面风格</span><select class="ui-settings-select" id="ui-scene"><option value="immersive">星空沉浸</option><option value="original">原图简洁</option></select></label>' +
          '<label class="ui-settings-row" for="ui-mode"><span>明暗模式</span><select class="ui-settings-select" id="ui-mode"><option value="dark">深色</option><option value="light">浅色</option></select></label>' +
          '<label class="ui-settings-row" for="ui-background"><span>显示聊天背景</span><input type="checkbox" id="ui-background"></label>' +
          '<label class="ui-settings-row" for="ui-characters"><span>显示侧栏人物</span><input type="checkbox" id="ui-characters" aria-describedby="ui-characters-desc"></label>' +
          '<p class="ui-settings-hint" id="ui-characters-desc">此开关只控制侧栏小人；背景中的人物随场景显示。</p>' +
        '</div>' +
        '<div class="ui-settings-range-group"><label class="ui-settings-range-label" for="ui-blur"><span>背景模糊</span><output id="ui-blur-value" for="ui-blur"></output></label><input type="range" id="ui-blur" min="0" max="8" step="1"></div>' +
        '<div class="ui-settings-range-group"><label class="ui-settings-range-label" for="ui-shade"><span>背景遮罩</span><output id="ui-shade-value" for="ui-shade"></output></label><input type="range" id="ui-shade" min="0" max="75" step="5"></div>' +
        '<p class="ui-settings-note">自动保存，仅调整当前浏览器的聊天页面。</p>' +
      '</div>' +
      '<div class="ui-settings-footer"><button type="button" id="ui-reset">恢复默认外观</button><button type="button" class="ui-settings-done">完成</button></div>' +
    '</section>';
  document.body.appendChild(layer);
  const app = document.getElementById("app");
  const select = selector => layer.querySelector(selector);
  const scene = select("#ui-scene");
  const mode = select("#ui-mode");
  const background = select("#ui-background");
  const characters = select("#ui-characters");
  const blur = select("#ui-blur");
  const shade = select("#ui-shade");
  const closeButton = select(".ui-settings-close");
  let returnFocus = trigger;
  let priorInert = false;

  function applyPreferences(save = false) {
    preferences.shade = preferences.sceneShades[preferences.scene];
    document.body.dataset.uiScene = preferences.scene;
    document.documentElement.dataset.uiPalette = preferences.palette;
    document.body.dataset.uiCharacters = preferences.characters ? "on" : "off";
    document.documentElement.style.setProperty("--ui-background-blur", preferences.blur + "px");
    document.documentElement.style.setProperty("--ui-background-shade", preferences.shade / 100);
    if (save) {
      try { localStorage.setItem(storageKey, JSON.stringify(preferences)); } catch {}
    }
  }
  function syncControls() {
    layer.querySelectorAll("[data-palette]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.palette === preferences.palette));
    });
    scene.value = preferences.scene;
    mode.value = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    background.checked = document.body.dataset.chatbg === "on";
    characters.checked = preferences.characters;
    blur.value = String(preferences.blur);
    shade.value = String(preferences.shade);
    select("#ui-blur-value").textContent = preferences.blur === 0 ? "清晰" : preferences.blur + " 级";
    select("#ui-shade-value").textContent = preferences.shade + "%";
    blur.disabled = shade.disabled = !background.checked;
  }
  function openSettings(event) {
    if (!layer.hidden) return;
    returnFocus = triggers.includes(event?.currentTarget) ? event.currentTarget : document.activeElement;
    syncControls();
    layer.hidden = false;
    priorInert = !!app?.inert;
    if (app) app.inert = true;
    triggers.forEach(button => button.setAttribute("aria-expanded", "true"));
    closeButton.focus();
  }
  function closeSettings() {
    if (layer.hidden) return;
    layer.hidden = true;
    if (app) app.inert = priorInert;
    triggers.forEach(button => button.setAttribute("aria-expanded", "false"));
    (returnFocus?.isConnected ? returnFocus : trigger).focus();
  }
  triggers.forEach(button => {
    button.title = "界面设置";
    button.setAttribute("aria-label", "界面设置");
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-controls", layer.id);
    button.setAttribute("aria-expanded", "false");
    button.onclick = openSettings;
  });
  closeButton.onclick = closeSettings;
  select(".ui-settings-done").onclick = closeSettings;
  layer.addEventListener("click", event => { if (event.target === layer) closeSettings(); });
  layer.querySelectorAll("[data-palette]").forEach(button => {
    button.onclick = () => {
      preferences.palette = button.dataset.palette;
      applyPreferences(true);
      syncControls();
    };
  });
  scene.onchange = () => { preferences.scene = scene.value; applyPreferences(true); syncControls(); };
  mode.onchange = () => { applyTheme(mode.value); syncControls(); };
  background.onchange = () => { applyChatBg(background.checked); syncControls(); };
  characters.onchange = () => { preferences.characters = characters.checked; applyPreferences(true); syncControls(); };
  blur.oninput = () => { preferences.blur = Number(blur.value); applyPreferences(true); syncControls(); };
  shade.oninput = () => { preferences.sceneShades[preferences.scene] = Number(shade.value); applyPreferences(true); syncControls(); };
  select("#ui-reset").onclick = () => {
    preferences = { ...defaults, sceneShades: { ...sceneShadeDefaults } };
    applyPreferences(true);
    applyTheme("dark");
    applyChatBg(true);
    syncControls();
  };
  layer.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeSettings(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...layer.querySelectorAll("button,input,select")].filter(element => !element.disabled && element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  function updateMobileViewport() {
    const viewport = window.visualViewport;
    const mobileLayout = window.innerWidth <= 768 || (window.innerWidth <= 1024 && window.innerHeight <= 600);
    if (viewport && mobileLayout && viewport.scale === 1) {
      document.documentElement.style.setProperty("--ui-viewport-height", Math.round(viewport.height) + "px");
      const layoutHeight = Math.max(window.innerHeight, document.documentElement.clientHeight);
      document.body.dataset.uiKeyboard = layoutHeight - viewport.height > 140 ? "on" : "off";
    } else {
      document.documentElement.style.removeProperty("--ui-viewport-height");
      document.body.dataset.uiKeyboard = "off";
    }
  }
  window.visualViewport?.addEventListener("resize", updateMobileViewport, { passive: true });
  window.addEventListener("resize", updateMobileViewport, { passive: true });
  applyPreferences(firstV2Activation || needsSceneShadeMigration);
  if (firstV2Activation) applyChatBg(true);
  updateMobileViewport();
})();

