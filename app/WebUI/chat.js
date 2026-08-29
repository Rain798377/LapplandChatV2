(function () {
  "use strict";

  const HUES = { bot: 289 };
  const SWATCH_HUES = [289, 250, 200, 150, 60, 30, 340];
  // Only used once, to migrate anyone's pre-existing local profile up to
  // the server the first time they load post-upgrade -- see
  // migrateLocalProfile(). Not written to anymore; the server (core/auth.py's
  // avatar/banner/hue/description columns) is the source of truth now, so
  // the same profile follows an account across devices instead of staying
  // stuck in whichever browser set it.
  const PROFILE_KEY = "lappland.webui.profile.v2";
  const POLL_MS = 3000;
  const USERS_POLL_MS = 8000; // the member list changes far less often than messages -- no need to hit it as hard

  // Every channel is shared/persisted server-side (core/chat_store.py) --
  // everyone viewing a channel sees the same messages. Only "general" also
  // gets bot replies; the other three are shared note-taking with no LLM
  // call. Must stay in sync with WEBUI_CHANNELS/WEBUI_BOT_CHANNEL in
  // core/config.py.
  const CHANNELS = [
    { key: "general", name: "general", topic: "Public channel -- Lappland listens and replies here.", botReplies: true },
    { key: "bug-reports", name: "bug-reports", topic: "Something broke? Note it here. No bot replies.", botReplies: false },
    { key: "feature-suggestions", name: "feature-suggestions", topic: "Half-formed ideas welcome. No bot replies.", botReplies: false },
    { key: "off-topic", name: "off-topic", topic: "Anything else -- no bot here.", botReplies: false },
  ];

  // Discord-style "/" command picker -- must stay in sync with
  // webui_commands.py's COMMANDS/cmd_help (name, description, arg shape) and
  // core/llm.py's DEFAULT_PROVIDER_CHAIN (ai-provider's choices), same
  // pragmatic tradeoff as CHANNELS above. isAdmin commands are hidden from
  // the popup for non-admins (the server enforces this regardless -- see
  // _require_admin in webui_commands.py -- this just avoids offering an
  // action that'll bounce with "you're not an administrator.").
  // Shared by /imagine and /imagine_anime -- must stay in sync with
  // webui_commands.py's _IMAGINE_FLAGS. Params with a `flag` are rendered as
  // "--flag value" (any order, only when filled in); the one without a flag
  // (prompt) is the plain leading positional text, exactly what
  // parse_imagine_args() expects.
  const IMAGINE_PARAMS = [
    { name: "prompt", required: true, wide: true },
    { name: "width", flag: "--width" },
    { name: "height", flag: "--height" },
    { name: "steps", flag: "--steps" },
    { name: "cfg", flag: "--cfg" },
    { name: "seed", flag: "--seed" },
    { name: "negative", flag: "--negative", wide: true },
  ];

  const COMMAND_SPECS = [
    { name: "help", desc: "this list", params: [] },
    { name: "ping", desc: "check the server responds", params: [] },
    { name: "time", desc: "current server time", params: [] },
    { name: "mood", desc: "the bot's current mood", params: [] },
    { name: "change_mood", desc: "set the bot's mood", isAdmin: true, params: [
      { name: "mood", required: true, wide: true },
    ] },
    { name: "ai-provider", desc: "pin or unpin the LLM provider", isAdmin: true, params: [
      { name: "provider", required: true, choices: ["auto", "groq", "gemini", "cloudflare", "mistral", "openrouter"] },
    ] },
    { name: "echo", desc: "echo it back", params: [
      { name: "text", required: true, wide: true },
    ] },
    { name: "curl", desc: "GET a URL and show the response", params: [
      { name: "url", required: true, wide: true },
    ] },
    { name: "ip", desc: "the server's public IP", params: [] },
    { name: "8ball", desc: "ask the magic 8-ball", params: [
      { name: "question", required: true, wide: true },
    ] },
    { name: "ship", desc: "compatibility rating", params: [
      { name: "name1", required: true },
      { name: "name2", required: true },
    ] },
    { name: "random", desc: "random number, coin, die, choice, or word", params: [
      { name: "type", required: true, choices: ["number", "coin", "die", "choice", "word"] },
      { name: "value", required: false, wide: true },
    ] },
    { name: "memory", desc: "manage what the bot remembers about you", params: [
      { name: "action", required: true, choices: ["wipe", "wipe-all", "edit", "view"] },
      { name: "notes", required: false, wide: true },
    ] },
    { name: "imagine", desc: "generate an image with FLUX.1-schnell", params: IMAGINE_PARAMS },
    { name: "imagine_anime", desc: "generate an image with Filigree-Anima", params: IMAGINE_PARAMS },
  ];

  const state = {
    draft: "", thinking: false, infoOpen: true, settingsOpen: false,
    active: "general",
    messages: {}, loaded: {}, lastIds: {}, seenIds: {},
    profile: { hue: null, photo: "", banner: "", desc: "" },
    botName: "Lappland", model: "…", mood: "…", lastProvider: null, connected: true,
    botAvatar: "", botBanner: "", botBio: "", botAbout: "", publicHomepage: "",
    users: [],
    profileCard: null, // the member object (see renderSidebar) currently shown in the profile card popup, or null
    account: null,
    editingId: null, editDraft: "",
    // slash-command popup + parameter boxes -- see updateSlashUI/selectCommand
    slash: { open: false, filtered: [], index: 0 },
    param: null, // the COMMAND_SPECS entry currently shown as boxes, or null
    paramValues: [],
  };
  CHANNELS.forEach(function (c) {
    state.messages[c.key] = [];
    state.loaded[c.key] = false;
    state.lastIds[c.key] = 0;
    state.seenIds[c.key] = new Set();
  });

  // ── tiny DOM builder -- textContent for anything user-controlled, never
  // innerHTML, so message bodies/usernames can't inject markup. ──
  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === undefined || v === null || v === false) continue;
        if (k === "text") node.textContent = v;
        else if (k === "style") node.style.cssText = v;
        else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v);
      }
    }
    if (children) {
      [].concat(children).forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  function hueForName(name) {
    let hash = 0;
    const s = name || "";
    for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) % 360;
    return hash;
  }

  function formatTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function avatarStyle(hue, photo, size) {
    const s = size || 30;
    let css = "width:" + s + "px;height:" + s + "px;border-radius:50%;flex:none;display:grid;place-items:center;font-size:" + Math.round(s * 0.4) + "px;font-weight:600;overflow:hidden;";
    if (photo) css += "background-image:url(" + JSON.stringify(photo) + ");background-size:cover;background-position:center;color:transparent;";
    else css += "background:oklch(0.45 0.07 " + hue + ");color:oklch(0.96 0.02 " + hue + ");";
    return css;
  }

  function badge(name, photo) {
    return photo ? "" : (name || "?").slice(0, 1).toUpperCase();
  }

  // ── DOM refs ──
  const els = {
    app: document.getElementById("app"),
    botNameLabel: document.getElementById("botNameLabel"),
    modelLabel: document.getElementById("modelLabel"),
    channelList: document.getElementById("channelList"),
    channelHighlight: document.getElementById("channelHighlight"),
    resetBtn: document.getElementById("resetBtn"),
    youAvatar: document.getElementById("youAvatar"),
    youName: document.getElementById("youName"),
    statusDot: document.getElementById("statusDot"),
    activeName: document.getElementById("activeName"),
    statusLine: document.getElementById("statusLine"),
    detailsBtn: document.getElementById("detailsBtn"),
    messageScroll: document.getElementById("messageScroll"),
    conversationLabel: document.getElementById("conversationLabel"),
    messageList: document.getElementById("messageList"),
    typingRow: document.getElementById("typingRow"),
    typingAvatar: document.getElementById("typingAvatar"),
    typingLabel: document.getElementById("typingLabel"),
    infoPanel: document.getElementById("infoPanel"),
    runtimeList: document.getElementById("runtimeList"),
    aboutText: document.getElementById("aboutText"),
    onlineCount: document.getElementById("onlineCount"),
    onlineMembersList: document.getElementById("onlineMembersList"),
    offlineMembersSection: document.getElementById("offlineMembersSection"),
    offlineCount: document.getElementById("offlineCount"),
    offlineMembersList: document.getElementById("offlineMembersList"),
    draftInput: document.getElementById("draftInput"),
    sendBtn: document.getElementById("sendBtn"),
    charCount: document.getElementById("charCount"),
    slashPopup: document.getElementById("slashPopup"),
    slashPopupList: document.getElementById("slashPopupList"),
    paramBar: document.getElementById("paramBar"),
    paramBarTitle: document.getElementById("paramBarTitle"),
    paramBarClose: document.getElementById("paramBarClose"),
    paramBoxes: document.getElementById("paramBoxes"),
    accountBtn: document.getElementById("accountBtn"),
    settingsBackdrop: document.getElementById("settingsBackdrop"),
    settingsDialog: document.getElementById("settingsDialog"),
    settingsCloseX: document.getElementById("settingsCloseX"),
    settingsDoneBtn: document.getElementById("settingsDoneBtn"),
    accountLine: document.getElementById("accountLine"),
    adminPanelLink: document.getElementById("adminPanelLink"),
    gateExitBtn: document.getElementById("gateExitBtn"),
    logoutBtn: document.getElementById("logoutBtn"),
    bigAvatar: document.getElementById("bigAvatar"),
    pickPhotoBtn: document.getElementById("pickPhotoBtn"),
    clearPhotoBtn: document.getElementById("clearPhotoBtn"),
    photoInput: document.getElementById("photoInput"),
    swatchList: document.getElementById("swatchList"),
    bannerPreview: document.getElementById("bannerPreview"),
    pickBannerBtn: document.getElementById("pickBannerBtn"),
    clearBannerBtn: document.getElementById("clearBannerBtn"),
    bannerInput: document.getElementById("bannerInput"),
    descInput: document.getElementById("descInput"),
    saveDescBtn: document.getElementById("saveDescBtn"),
    storageNote: document.getElementById("storageNote"),
    clearHistoryBtn: document.getElementById("clearHistoryBtn"),
    resetAllBtn: document.getElementById("resetAllBtn"),
    profileBackdrop: document.getElementById("profileBackdrop"),
    profileBanner: document.getElementById("profileBanner"),
    profileAvatar: document.getElementById("profileAvatar"),
    profileName: document.getElementById("profileName"),
    profileUserId: document.getElementById("profileUserId"),
    profileDesc: document.getElementById("profileDesc"),
    profileEditBtn: document.getElementById("profileEditBtn"),
    profileCloseBtn: document.getElementById("profileCloseBtn"),
  };

  function activeChannel() {
    return CHANNELS.find(function (c) { return c.key === state.active; }) || CHANNELS[0];
  }

  // ── profile (avatar/banner/hue/description) -- saved server-side (see
  // core/auth.py's update_profile, POST /api/profile) so it follows the
  // account across devices/browsers instead of being stuck wherever it was
  // last set. ──
  // patch uses this file's own field names (photo, desc); the server's
  // columns are named avatar/description (see core/auth.py) -- translated
  // at this one boundary rather than renaming either side to match, so the
  // (much larger) rest of chat.js never has to know the server spells them
  // differently.
  function setProfile(patch) {
    Object.assign(state.profile, patch);
    render();
    const body = {};
    if ("photo" in patch) body.avatar = patch.photo;
    if ("banner" in patch) body.banner = patch.banner;
    if ("hue" in patch) body.hue = patch.hue;
    if ("desc" in patch) body.description = patch.desc;
    fetch("api/profile", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(function () { /* best-effort -- the local UI already updated; a lost save just means it doesn't follow to another device this time */ });
  }

  // One-time upgrade path: anyone whose profile was still living in this
  // browser's localStorage from before the server-side columns existed
  // gets it pushed up automatically the first time they load post-upgrade,
  // instead of silently losing whatever avatar/banner/description they'd
  // already set. Only runs when the server has nothing yet, so it can't
  // clobber a profile already saved (e.g. from another device).
  function migrateLocalProfile() {
    const hasServerProfile = state.profile.photo || state.profile.banner || state.profile.desc || state.profile.hue != null;
    if (hasServerProfile) return;
    try {
      const saved = JSON.parse(localStorage.getItem(PROFILE_KEY) || "null");
      if (saved && (saved.photo || saved.banner || saved.desc || saved.hue != null)) {
        setProfile(saved);
      }
      localStorage.removeItem(PROFILE_KEY);
    } catch (e) { /* ignore */ }
  }

  // ── auth ──
  function checkAuth() {
    fetch("api/me")
      .then(function (res) { if (!res.ok) throw new Error("unauthenticated"); return res.json(); })
      .then(function (data) {
        state.account = { userId: data.user_id, username: data.username, isAdmin: !!data.isAdmin };
        state.profile.photo = data.avatar || "";
        state.profile.banner = data.banner || "";
        state.profile.hue = data.hue != null ? data.hue : null;
        state.profile.desc = data.description || "";
        migrateLocalProfile();
        document.body.classList.add("ready");
        refreshInfo();
        refreshUsers();
        loadChannel(state.active);
        setInterval(pollActive, POLL_MS);
        setInterval(refreshUsers, USERS_POLL_MS);
        render();
      })
      .catch(function () { window.location.href = "login.html"; });
  }

  function logout() {
    fetch("api/logout", { method: "POST" }).finally(function () { window.location.href = "login.html"; });
  }

  // Clears the "~"-terminal gate cookie (see webui_server.py's POST
  // /gate/exit) and goes back to "./", which -- now that the cookie's gone
  // -- serves cover.html instead of this chat shell. Your real session
  // (login) is untouched; this only re-hides the app behind the cover page.
  function gateExit() {
    // A relative "./" only ever lands back on whatever host you're
    // currently on -- a no-op if that host is WEBUI_GATE_BYPASS_HOST,
    // since that one always serves chat regardless of the gate cookie (see
    // webui_server.py's index()). state.publicHomepage (WEBUI_PUBLIC_HOMEPAGE)
    // is an absolute URL for exactly that case; falls back to "./" if unset.
    fetch("gate/exit", { method: "POST" }).finally(function () {
      window.location.href = state.publicHomepage || "./";
    });
  }

  // ── channel data ──
  function loadChannel(key) {
    fetch("api/messages/" + encodeURIComponent(key))
      .then(function (res) { if (!res.ok) throw new Error("bad response"); return res.json(); })
      .then(function (data) {
        const msgs = data.messages || [];
        state.messages[key] = msgs;
        state.loaded[key] = true;
        state.lastIds[key] = msgs.length ? msgs[msgs.length - 1].id : 0;
        // Mark the initial history as already "seen" so it renders in place
        // instead of the whole backlog popping in at once -- only messages
        // that arrive after this count as new (see renderMessages).
        state.seenIds[key] = new Set(msgs.map(function (m) { return m.id; }));
        render();
      })
      .catch(function () {});
  }

  // Signature of a message list's content (id + body + edited flag) --
  // used to skip re-rendering (and re-scrolling) when a poll finds nothing
  // actually changed.
  function messagesSignature(msgs) {
    return msgs.map(function (m) { return m.id + ":" + m.body + ":" + (m.edited ? 1 : 0); }).join("|");
  }

  // Short polling, not a push channel -- fine for a 10-20 person group (see
  // core/chat_store.py). Only polls the channel currently being viewed. A
  // full reconcile (not just id > since) so edits/deletes made by someone
  // else while you're looking at the channel show up too, not just brand
  // new messages.
  function pollActive() {
    const key = state.active;
    if (!state.loaded[key] || state.editingId != null) return;
    fetch("api/messages/" + encodeURIComponent(key))
      .then(function (res) { if (!res.ok) throw new Error("bad response"); return res.json(); })
      .then(function (data) {
        const msgs = data.messages || [];
        if (messagesSignature(msgs) === messagesSignature(state.messages[key] || [])) return;
        state.messages[key] = msgs;
        state.lastIds[key] = msgs.length ? msgs[msgs.length - 1].id : 0;
        render();
      })
      .catch(function () {});
  }

  function refreshInfo() {
    fetch("api/info")
      .then(function (res) { if (!res.ok) throw new Error("bad response"); return res.json(); })
      .then(function (data) {
        state.botName = data.botName || state.botName;
        state.model = data.model || state.model;
        state.mood = data.mood || state.mood;
        state.lastProvider = data.lastProvider || null;
        state.botAvatar = data.botAvatar || "";
        state.botBanner = data.botBanner || "";
        state.botBio = data.botBio || "";
        state.botAbout = data.botAbout || "";
        state.publicHomepage = data.publicHomepage || "";
        state.connected = true;
        render();
      })
      .catch(function () { state.connected = false; render(); });
  }

  // ── slash command picker -- Discord-style: "/" opens a filtered list of
  // commands, picking one (click/Enter/Tab) swaps the textarea into a small
  // read-only preview and shows one labeled input box per parameter below
  // it. Typing in those boxes rebuilds the actual "/name arg1 arg2" text
  // that gets sent -- the boxes are just a friendlier way to build the same
  // plain-text command webui_commands.py already parses, no protocol change. ──
  function matchCommands(query) {
    const q = query.toLowerCase();
    return COMMAND_SPECS.filter(function (c) {
      if (c.isAdmin && !(state.account && state.account.isAdmin)) return false;
      return c.name.indexOf(q) === 0;
    });
  }

  function closeSlashPopup() {
    state.slash = { open: false, filtered: [], index: 0 };
    els.slashPopup.style.display = "none";
  }

  function closeParamBar() {
    state.param = null;
    state.paramValues = [];
    els.paramBar.style.display = "none";
    els.draftInput.readOnly = false;
  }

  function renderSlashPopup() {
    els.slashPopup.style.display = state.slash.open && state.slash.filtered.length ? "block" : "none";
    if (!state.slash.open) return;
    els.slashPopupList.innerHTML = "";
    state.slash.filtered.forEach(function (spec, i) {
      const active = i === state.slash.index;
      els.slashPopupList.appendChild(h("div", {
        style: "display:flex;align-items:baseline;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;background:" +
          (active ? "color-mix(in srgb, var(--color-accent) 15%, transparent)" : "transparent") + ";",
        onmouseenter: function () { state.slash.index = i; renderSlashPopup(); },
        onclick: function () { selectCommand(spec); },
      }, [
        h("span", { style: "font-family:var(--font-heading);font-size:13.5px;font-weight:600;color:var(--color-accent);white-space:nowrap;", text: "/" + spec.name }),
        h("span", { style: "font-size:12.5px;color:color-mix(in srgb, var(--color-text) 60%, transparent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", text: spec.desc }),
      ]));
    });
  }

  // Re-evaluates popup/param-box visibility from the current draft text --
  // called on every keystroke in the textarea (not while a param box has
  // taken over, see openParamBar).
  function updateSlashUI() {
    if (state.param) return; // boxes own the draft text while open
    const m = /^\/([a-zA-Z0-9_-]*)$/.exec(state.draft);
    if (!m) { closeSlashPopup(); return; }
    const filtered = matchCommands(m[1]);
    state.slash = { open: true, filtered: filtered, index: 0 };
    renderSlashPopup();
  }

  function openParamBar(spec, prefillValues) {
    closeSlashPopup();
    state.param = spec;
    state.paramValues = prefillValues || spec.params.map(function () { return ""; });
    els.paramBarTitle.textContent = "/" + spec.name + " — " + spec.desc;
    els.paramBoxes.innerHTML = "";
    spec.params.forEach(function (p, i) {
      const box = h("input", {
        class: "input", type: "text",
        style: "flex:" + (p.wide ? "1 1 220px" : "0 1 130px") + ";min-width:100px;font-size:13px;padding:6px 9px;",
        placeholder: p.name + (p.required ? " *" : ""),
        value: state.paramValues[i] || "",
        oninput: function (e) { state.paramValues[i] = e.target.value; syncDraftFromParams(); },
        onkeydown: function (e) {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          else if (e.key === "Escape") { e.preventDefault(); cancelParamBar(); }
        },
      });
      if (p.choices) box.setAttribute("list", "params-" + spec.name + "-" + p.name);
      els.paramBoxes.appendChild(box);
      if (p.choices) {
        const dl = h("datalist", { id: "params-" + spec.name + "-" + p.name });
        p.choices.forEach(function (c) { dl.appendChild(h("option", { value: c })); });
        els.paramBoxes.appendChild(dl);
      }
    });
    els.draftInput.readOnly = spec.params.length > 0;
    els.paramBar.style.display = "flex";
    syncDraftFromParams();
    const firstBox = els.paramBoxes.querySelector("input");
    if (firstBox) firstBox.focus(); else els.draftInput.focus();
  }

  function syncDraftFromParams() {
    const spec = state.param;
    const parts = [];
    spec.params.forEach(function (p, i) {
      const v = (state.paramValues[i] || "").trim();
      if (!v) return; // skip empty params entirely, flagged or positional
      if (p.flag) parts.push(p.flag, /\s/.test(v) ? JSON.stringify(v) : v);
      else parts.push(v);
    });
    const text = "/" + spec.name + (parts.length ? " " + parts.join(" ") : "");
    state.draft = text;
    els.draftInput.value = text;
    renderComposer();
  }

  function cancelParamBar() {
    closeParamBar();
    els.draftInput.focus();
  }

  function selectCommand(spec) {
    if (spec.params.length === 0) {
      state.draft = "/" + spec.name + " ";
      els.draftInput.value = state.draft;
      closeSlashPopup();
      els.draftInput.focus();
      els.draftInput.setSelectionRange(state.draft.length, state.draft.length);
      renderComposer();
      return;
    }
    openParamBar(spec, null);
  }

  els.paramBarClose.addEventListener("click", cancelParamBar);

  // ── sending ──
  function send() {
    const text = state.draft.trim();
    if (!text) return;
    const chan = activeChannel();
    if (chan.botReplies && state.thinking) return;

    closeSlashPopup();
    closeParamBar();
    state.draft = "";
    state.thinking = chan.botReplies;
    els.draftInput.value = "";
    render();

    const active = state.active;
    fetch("api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, channel: active }),
    })
      .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (result) {
        if (!result.ok) { state.thinking = false; state.connected = false; render(); return; }
        const data = result.data;
        // A slow reply (e.g. /imagine, which edits its own message in
        // place for minutes while generating -- see webui_server.py's
        // _run_imagine_command) can already have been picked up by a
        // routine pollActive() tick before this fetch resolves. Upsert by
        // id rather than blindly pushing, or it'd render twice.
        const msgs = state.messages[active].slice();
        function upsert(m) {
          const i = msgs.findIndex(function (x) { return x.id === m.id; });
          if (i === -1) msgs.push(m); else msgs[i] = m;
        }
        let lastId = state.lastIds[active] || 0;
        if (data.message) { upsert(data.message); lastId = Math.max(lastId, data.message.id); }
        if (data.reply) { upsert(data.reply); lastId = Math.max(lastId, data.reply.id); }
        state.thinking = false;
        state.connected = true;
        state.mood = data.mood || state.mood;
        state.lastProvider = data.provider || state.lastProvider;
        state.messages[active] = msgs;
        state.lastIds[active] = lastId;
        render();
      })
      .catch(function () { state.thinking = false; state.connected = false; render(); });
  }

  // ── admin resets ──
  function resetChannelOnServer(key) {
    return fetch("api/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel: key }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) { return { key: key, greeting: data.greeting || null }; });
  }
  function applyResets(results) {
    results.forEach(function (r) {
      state.messages[r.key] = r.greeting ? [r.greeting] : [];
      state.lastIds[r.key] = r.greeting ? r.greeting.id : 0;
    });
    render();
  }
  function resetConversation() {
    resetChannelOnServer(state.active).then(function (r) { applyResets([r]); });
  }
  function clearHistory() {
    Promise.all(CHANNELS.map(function (c) { return resetChannelOnServer(c.key); })).then(applyResets);
  }
  function resetAll() {
    if (state.account && state.account.isAdmin) clearHistory();
    setProfile({ hue: null, photo: "", banner: "", desc: "" });
  }

  // ── per-message edit/delete -- own messages, or any message if admin
  // (see _can_modify in webui_server.py) ──
  function canModify(m) {
    const isAdmin = !!(state.account && state.account.isAdmin);
    return isAdmin || (!m.bot && state.account && m.userId === state.account.userId);
  }

  function startEdit(m) {
    state.editingId = m.id;
    state.editDraft = m.body;
    render();
  }

  function cancelEdit() {
    state.editingId = null;
    state.editDraft = "";
    render();
  }

  function saveEdit(id) {
    const body = state.editDraft.trim();
    if (!body) return;
    fetch("api/messages/" + id + "/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: body }),
    })
      .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (result) {
        if (!result.ok) return;
        const key = state.active;
        state.messages[key] = state.messages[key].map(function (m) {
          return m.id === id ? result.data.message : m;
        });
        state.editingId = null;
        state.editDraft = "";
        render();
      })
      .catch(function () {});
  }

  function deleteMessage(id, skipConfirm) {
    if (!skipConfirm && !window.confirm("Delete this message? This can't be undone.")) return;
    fetch("api/messages/" + id + "/delete", { method: "POST" })
      .then(function (res) { if (!res.ok) throw new Error("failed"); return res.json(); })
      .then(function () {
        const key = state.active;
        state.messages[key] = state.messages[key].filter(function (m) { return m.id !== id; });
        render();
      })
      .catch(function () {});
  }

  // ── photo upload -- openImageCropper (cropper.js) lets the user drag/zoom
  // before it's downscaled to a square data URL ──
  function onPhoto(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!f) return;
    window.openImageCropper(f).then(function (dataUrl) {
      if (dataUrl) setProfile({ photo: dataUrl });
    });
  }

  function onBanner(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    window.openImageCropper(f, { width: 320, height: 100, round: false, outputWidth: 960, outputHeight: 300 }).then(function (dataUrl) {
      if (dataUrl) setProfile({ banner: dataUrl });
    });
  }

  function saveDesc() {
    setProfile({ desc: els.descInput.value });
  }

  // Opens the Settings dialog, initializing descInput's value first -- NOT
  // done in renderSettings() itself, since render() runs on a poll timer
  // (pollActive) independent of whether this dialog is open, and blindly
  // resetting a text input's .value on every render would blow away
  // whatever the user is mid-typing (and reset their cursor position).
  function openSettings() {
    els.descInput.value = state.profile.desc || "";
    state.settingsOpen = true;
    render();
  }

  // ── profile card -- click a member in the sidebar to see their (or the
  // bot's) avatar/banner/description, Discord-style. Only ever the bot or
  // yourself, since that's all the sidebar's "Here" list ever shows (see
  // renderSidebar) -- there's no live multi-user presence here. ──
  function openProfileCard(member) {
    state.profileCard = member;
    render();
  }

  function closeProfileCard() {
    state.profileCard = null;
    render();
  }

  // ── rendering ──
  function scrollDown() {
    els.messageScroll.scrollTop = els.messageScroll.scrollHeight;
  }

  function renderSidebar() {
    els.botNameLabel.textContent = state.botName;
    els.modelLabel.textContent = state.model;

    els.channelList.innerHTML = "";
    let activeBtn = null;
    CHANNELS.forEach(function (c) {
      const isActive = c.key === state.active;
      // Background comes from the persistent #channelHighlight bar sliding
      // behind these buttons (see below), not from each button's own style
      // -- that's what lets it animate instead of jumping, since these
      // button elements themselves get torn down and rebuilt every render.
      const btn = h("button", {
        class: "sidebar-item",
        style: "position:relative;z-index:1;display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;cursor:pointer;font-size:14px;padding:6px 8px;border-radius:8px;background:transparent;color:" +
          (isActive ? "var(--color-accent)" : "color-mix(in srgb, var(--color-text) 70%, transparent)") + ";",
        onclick: function () {
          state.active = c.key;
          closeSlashPopup();
          closeParamBar();
          if (!state.loaded[c.key]) loadChannel(c.key);
          render();
        },
      }, [
        h("span", { style: "opacity:0.45;" }, "#"),
        h("span", { style: "flex:1;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", text: c.name }),
        c.botReplies ? h("span", { style: "width:6px;height:6px;border-radius:50%;background:oklch(0.72 0.14 145);" }) : null,
      ]);
      els.channelList.appendChild(btn);
      if (isActive) activeBtn = btn;
    });
    if (activeBtn) {
      els.channelHighlight.style.transform = "translateY(" + activeBtn.offsetTop + "px)";
      els.channelHighlight.style.height = activeBtn.offsetHeight + "px";
    }

    const myUsername = state.account ? state.account.username : null;
    const myHue = state.profile.hue != null ? state.profile.hue : hueForName(myUsername || "");

    const isAdmin = !!(state.account && state.account.isAdmin);
    els.resetBtn.style.display = isAdmin ? "flex" : "none";
    els.resetBtn.textContent = "Reset #" + activeChannel().name;
    els.youAvatar.style.cssText = avatarStyle(myHue, state.profile.photo, 26);
    els.youAvatar.textContent = badge(myUsername, state.profile.photo);
    els.youName.textContent = myUsername || "…";
  }

  function renderHeader() {
    const chan = activeChannel();
    els.statusDot.style.background = state.connected ? "oklch(0.72 0.14 145)" : "oklch(0.6 0.18 25)";
    els.activeName.textContent = chan.name;
    els.statusLine.textContent = chan.botReplies
      ? (state.connected ? (state.mood + " · via " + (state.lastProvider || "…")) : "backend unreachable")
      : chan.topic;
    els.conversationLabel.textContent = chan.botReplies ? ("Conversation with " + state.botName) : ("#" + chan.name);
  }

  function renderMessages() {
    const chan = activeChannel();
    const myUsername = state.account ? state.account.username : null;
    const myHue = state.profile.hue != null ? state.profile.hue : hueForName(myUsername || "");
    const msgs = state.messages[state.active] || [];

    // Only messages that arrived since the last render animate in -- avoids
    // replaying the entrance pop on the whole history every poll/re-render
    // (see loadChannel, which pre-seeds this on initial load).
    const seen = state.seenIds[state.active] || new Set();

    els.messageList.innerHTML = "";
    msgs.forEach(function (m, i) {
      const prev = msgs[i - 1];
      const grouped = !!(prev && prev.author === m.author && !prev.stats);
      const isMe = !m.bot && m.author === myUsername;
      const other = (!m.bot && !isMe) ? state.users.find(function (u) { return u.userId === m.userId; }) : null;
      const hue = m.bot ? HUES.bot : (isMe ? myHue : (other && other.hue != null ? other.hue : hueForName(m.author)));
      const photo = m.bot ? state.botAvatar : (isMe ? state.profile.photo : (other ? (other.avatar || "") : ""));
      const editing = state.editingId === m.id;
      const isNew = !seen.has(m.id);

      const children = [];
      if (!grouped) {
        children.push(h("div", { style: "display:flex;align-items:baseline;gap:9px;margin-bottom:2px;" }, [
          h("span", { style: "font-family:var(--font-heading);font-weight:600;font-size:16px;color:oklch(0.82 0.05 " + hue + ");", text: m.author }),
          m.bot ? h("span", { class: "tag tag-accent", style: "font-size:9.5px;letter-spacing:0.06em;padding:2px 6px;", text: "BOT" }) : null,
          h("span", { style: "font-size:11.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);", text: formatTime(m.createdAt) }),
          m.edited ? h("span", { style: "font-size:11.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);font-style:italic;", text: "(edited)" }) : null,
        ]));
      }

      if (editing) {
        children.push(h("textarea", {
          style: "width:100%;min-height:60px;resize:vertical;background:var(--color-bg);border:1px solid var(--color-accent);border-radius:8px;color:var(--color-text);font-size:15px;font-family:inherit;padding:8px 10px;",
          oninput: function (e) { state.editDraft = e.target.value; },
          onkeydown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveEdit(m.id); }
            else if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
          },
          text: state.editDraft,
        }));
        children.push(h("div", { style: "display:flex;gap:8px;margin-top:6px;" }, [
          h("button", { class: "btn btn-primary", style: "font-size:12.5px;padding:4px 12px;", onclick: function () { saveEdit(m.id); }, text: "Save" }),
          h("button", { class: "btn btn-ghost", style: "font-size:12.5px;padding:4px 12px;", onclick: cancelEdit, text: "Cancel" }),
        ]));
      } else {
        children.push(h("div", { style: "font-size:15.5px;line-height:1.5;color:var(--color-text);white-space:pre-wrap;overflow-wrap:anywhere;text-wrap:pretty;", text: m.body }));
        if (m.image) {
          // A plain <img> rather than a fixed-size background-image box --
          // that box was always 420x420 regardless of the generated image's
          // own aspect ratio, so a portrait/landscape image left a big block
          // of empty background on whichever side didn't fill the square.
          // display:block on an inline-sized element sizes to the image's
          // own intrinsic dimensions (capped by max-width/max-height), so
          // there's no leftover space to pad out.
          children.push(h("img", {
            src: m.image, alt: "generated image",
            style: "display:block;max-width:min(420px,100%);max-height:420px;width:auto;height:auto;border-radius:10px;margin-top:8px;border:1px solid var(--color-divider);background:var(--color-bg);",
          }));
        }
        if (m.stats) {
          children.push(h("div", { style: "font-size:11.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);margin-top:5px;", text: m.stats }));
        }
      }

      const actions = (!editing && canModify(m)) ? h("div", { class: "msg-actions", style: "display:flex;gap:2px;flex:none;" }, [
        h("button", { class: "btn btn-icon btn-ghost", style: "width:26px;height:26px;font-size:12px;", title: "Edit", onclick: function () { startEdit(m); }, text: "✎" }),
        h("button", {
          class: "btn btn-icon btn-ghost btn-delete", style: "width:26px;height:26px;font-size:12px;",
          title: "Delete (hold Shift to skip the confirmation)",
          onclick: function (e) { deleteMessage(m.id, e.shiftKey); },
          text: "🗑",
        }),
      ]) : null;

      const row = h("div", { class: "msg-row" + (isNew ? " msg-enter" : ""), style: "display:flex;gap:14px;padding:" + (grouped ? "2px" : "9px") + " 12px 3px 8px;border-radius:8px;" }, [
        h("div", { style: "width:46px;flex:none;padding-top:2px;" }, grouped ? null : h("div", { style: avatarStyle(hue, photo, 38), text: badge(m.author, photo) })),
        h("div", { style: "flex:1;min-width:0;" }, children),
        actions,
      ]);
      els.messageList.appendChild(row);
    });
    state.seenIds[state.active] = new Set(msgs.map(function (m) { return m.id; }));

    const showTyping = state.thinking && chan.botReplies;
    els.typingRow.style.display = showTyping ? "flex" : "none";
    if (showTyping) {
      els.typingAvatar.style.cssText = avatarStyle(HUES.bot, state.botAvatar, 38);
      els.typingAvatar.textContent = state.botName.slice(0, 1).toUpperCase();
      els.typingLabel.textContent = state.botName + " is typing";
    }

    scrollDown();
  }

  function renderInfoPanel() {
    els.infoPanel.style.display = state.infoOpen ? "flex" : "none";
    if (!state.infoOpen) return;
    const chan = activeChannel();
    const activeMessages = state.messages[state.active] || [];
    const rows = [
      { k: "model", v: state.model },
      { k: "served by", v: state.lastProvider || "…" },
      { k: "mood", v: state.mood },
      { k: "history", v: activeMessages.length + " in #" + chan.name },
    ];
    els.runtimeList.innerHTML = "";
    rows.forEach(function (r) {
      els.runtimeList.appendChild(h("div", { style: "display:flex;justify-content:space-between;gap:10px;font-size:13px;" }, [
        h("span", { style: "color:color-mix(in srgb, var(--color-text) 65%, transparent);", text: r.k }),
        h("span", { style: "font-size:12.5px;color:var(--color-text);text-align:right;", text: r.v }),
      ]));
    });
    // Default About text -- shown unless the admin set a custom one (see
    // admin.html's "About (details panel)" field). botBio is a separate,
    // shorter thing shown on the bot's profile card instead (see
    // renderProfileCard), not duplicated here.
    const defaultAbout = state.botName + " answers through " + state.model +
      ", falling back across Groq → Gemini → Cloudflare → Mistral → OpenRouter if a provider is rate-limited or down. " +
      "Type /help for text commands. #general is the only channel Lappland reads -- bug-reports, feature-suggestions and off-topic are just notes for you.";
    els.aboutText.textContent = state.botAbout && state.botAbout.trim() ? state.botAbout : defaultAbout;
  }

  // Builds the same "member" shape openProfileCard/renderProfileCard expect
  // (see the left-sidebar profile-card feature this replaced). api/users
  // (see refreshUsers) now carries every account's avatar/banner/hue/
  // description, so anyone else's profile renders from that; only your own
  // (state.profile) is used for yourself, since it reflects unsaved edits
  // immediately instead of waiting on the next refreshUsers poll.
  function toMember(u) {
    const isMe = !!(state.account && u.userId === state.account.userId);
    const myHue = state.profile.hue != null ? state.profile.hue : hueForName(state.account ? state.account.username : "");
    return {
      name: u.username, isBot: false, isMe: isMe, online: u.online,
      hue: isMe ? myHue : (u.hue != null ? u.hue : hueForName(u.username)),
      photo: isMe ? state.profile.photo : (u.avatar || ""),
      banner: isMe ? state.profile.banner : (u.banner || ""),
      desc: isMe ? state.profile.desc : (u.description || ""),
      userId: u.userId, isAdmin: u.isAdmin,
    };
  }

  function renderMembersList() {
    const botMember = {
      name: state.botName, isBot: true, isMe: false, online: true, hue: HUES.bot,
      photo: state.botAvatar, banner: state.botBanner, desc: state.botBio, userId: null, isAdmin: false,
    };
    const online = [botMember].concat(state.users.filter(function (u) { return u.online; }).map(toMember));
    const offline = state.users.filter(function (u) { return !u.online; }).map(toMember);

    function row(m) {
      return h("div", {
        class: "sidebar-item",
        style: "display:flex;align-items:center;gap:11px;padding:7.5px 6px;border-radius:10px;cursor:pointer;",
        onclick: function () { openProfileCard(m); },
      }, [
        h("div", { style: "position:relative;flex:none;" }, [
          h("div", { style: avatarStyle(m.hue, m.photo, 38), text: badge(m.name, m.photo) }),
          h("span", {
            style: "position:absolute;right:-1px;bottom:-1px;width:11px;height:11px;border-radius:50%;border:3px solid var(--color-surface);background:" +
              (m.online ? "oklch(0.72 0.14 145)" : "color-mix(in srgb, var(--color-text) 35%, transparent)") + ";",
          }),
        ]),
        h("div", { style: "flex:1;min-width:0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
          h("span", {
            style: "font-family:var(--font-heading);font-weight:600;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:" +
              (m.online ? "var(--color-text)" : "color-mix(in srgb, var(--color-text) 50%, transparent)") + ";",
            text: m.name,
          }),
          m.isBot ? h("span", { class: "tag tag-accent", style: "font-size:9px;letter-spacing:0.06em;padding:2px 6px;", text: "BOT" }) : null,
          m.isAdmin ? h("span", { class: "tag tag-accent", style: "font-size:9px;letter-spacing:0.06em;padding:2px 6px;", text: "ADMIN" }) : null,
        ]),
      ]);
    }

    els.onlineMembersList.innerHTML = "";
    online.forEach(function (m) { els.onlineMembersList.appendChild(row(m)); });
    els.offlineMembersList.innerHTML = "";
    offline.forEach(function (m) { els.offlineMembersList.appendChild(row(m)); });
    els.onlineCount.textContent = String(online.length);
    els.offlineCount.textContent = String(offline.length);
    els.offlineMembersSection.style.display = offline.length ? "flex" : "none";
  }

  function refreshUsers() {
    fetch("api/users")
      .then(function (res) { if (!res.ok) throw new Error("bad response"); return res.json(); })
      .then(function (data) {
        state.users = data.users || [];
        renderMembersList();
      })
      .catch(function () {});
  }

  function renderComposer() {
    els.charCount.textContent = state.draft.length ? (state.draft.length + " chars") : "";
    const hasText = !!state.draft.trim();
    els.sendBtn.style.cssText = hasText
      ? "flex:none;cursor:pointer;font-family:var(--font-heading);font-size:14px;font-weight:500;padding:8px 16px;border-radius:8px;background:transparent;border:1px solid var(--color-accent);color:var(--color-accent);"
      : "flex:none;cursor:default;font-family:var(--font-heading);font-size:14px;font-weight:500;padding:8px 16px;border-radius:8px;background:transparent;border:1px solid var(--color-divider);color:color-mix(in srgb, var(--color-text) 35%, transparent);";
    const chan = activeChannel();
    els.draftInput.placeholder = chan.botReplies ? ("Message " + state.botName + " (or /help for commands)") : ("Message #" + chan.name);
  }

  function renderSettings() {
    els.settingsBackdrop.style.display = state.settingsOpen ? "grid" : "none";
    if (!state.settingsOpen) return;

    const isAdmin = !!(state.account && state.account.isAdmin);
    const myUsername = state.account ? state.account.username : "";
    const myHue = state.profile.hue != null ? state.profile.hue : hueForName(myUsername || "");

    els.accountLine.innerHTML = "";
    els.accountLine.appendChild(document.createTextNode("Signed in as " + myUsername + " "));
    if (isAdmin) els.accountLine.appendChild(h("span", { class: "tag tag-accent", style: "font-size:9px;letter-spacing:0.06em;padding:2px 6px;", text: "ADMIN" }));
    els.adminPanelLink.style.display = isAdmin ? "inline-flex" : "none";

    els.bigAvatar.style.cssText = avatarStyle(myHue, state.profile.photo, 60);
    els.bigAvatar.textContent = badge(myUsername, state.profile.photo);

    els.swatchList.innerHTML = "";
    SWATCH_HUES.forEach(function (hue) {
      els.swatchList.appendChild(h("button", {
        style: "width:28px;height:28px;border-radius:50%;cursor:pointer;background:oklch(0.5 0.09 " + hue + ");border:2px solid " + (state.profile.hue === hue ? "var(--color-neutral-100)" : "transparent") + ";",
        onclick: function () { setProfile({ hue: hue }); },
      }));
    });

    // banner/desc aren't reset here on every render -- banner is a
    // discrete image swap (safe any time), but descInput is a live text
    // field someone might be mid-typing in; see openSettings() for why its
    // value is only ever initialized once, on open.
    els.bannerPreview.style.backgroundImage = state.profile.banner ? "url(" + JSON.stringify(state.profile.banner) + ")" : "none";

    els.storageNote.textContent = isAdmin
      ? "Messages in every channel are shared and saved on the server -- everyone signed in sees the same conversation. Your avatar/banner/description are saved to your account too, so they follow you to any device you sign in on, and show to anyone who clicks your name. You're admin, so the buttons below wipe the shared channels for everyone."
      : "Messages in every channel are shared and saved on the server -- everyone signed in sees the same conversation. Your avatar/banner/description are saved to your account too, so they follow you to any device you sign in on, and show to anyone who clicks your name. Wiping shared channels is admin-only.";
    els.clearHistoryBtn.style.display = isAdmin ? "inline-flex" : "none";
  }

  function renderProfileCard() {
    const m = state.profileCard;
    els.profileBackdrop.style.display = m ? "grid" : "none";
    if (!m) return;

    els.profileBanner.style.backgroundImage = m.banner ? "url(" + JSON.stringify(m.banner) + ")" : "none";
    // margin-top pulls the avatar up to overlap the banner's bottom edge,
    // Discord-style; box-shadow acts as a ring "cutting into" the banner.
    els.profileAvatar.style.cssText = "margin-top:-46px;box-shadow:0 0 0 4px var(--color-surface);" + avatarStyle(m.hue, m.photo, 92);
    els.profileAvatar.textContent = badge(m.name, m.photo);

    els.profileName.innerHTML = "";
    els.profileName.appendChild(document.createTextNode(m.name));
    if (m.isAdmin) els.profileName.appendChild(h("span", { class: "tag tag-accent", style: "font-size:10.5px;letter-spacing:0.06em;padding:2px 7px;", text: "ADMIN" }));

    els.profileUserId.style.display = m.userId ? "block" : "none";
    els.profileUserId.textContent = m.userId ? "ID: " + m.userId : "";

    els.profileDesc.textContent = m.desc || (m.isBot ? "No bio set." : "No description set.");
    els.profileEditBtn.style.display = m.isMe ? "inline-flex" : "none";
  }

  function render() {
    renderSidebar();
    renderHeader();
    renderMessages();
    renderInfoPanel();
    renderMembersList(); // always-visible right panel, independent of the "details" toggle
    renderComposer();
    renderSettings();
    renderProfileCard();
  }

  // ── shift-key tracking -- a class on <body> so a delete button's :hover
  // style can preview "this skips the confirmation" while Shift is held. ──
  document.addEventListener("keydown", function (e) { if (e.key === "Shift") document.body.classList.add("shift-down"); });
  document.addEventListener("keyup", function (e) { if (e.key === "Shift") document.body.classList.remove("shift-down"); });
  window.addEventListener("blur", function () { document.body.classList.remove("shift-down"); });

  // ── static event wiring (once) ──
  els.detailsBtn.addEventListener("click", function () { state.infoOpen = !state.infoOpen; render(); });
  els.accountBtn.addEventListener("click", openSettings);
  els.settingsCloseX.addEventListener("click", function () { state.settingsOpen = false; render(); });
  els.settingsDoneBtn.addEventListener("click", function () { state.settingsOpen = false; render(); });
  els.settingsBackdrop.addEventListener("click", function (e) {
    if (e.target === els.settingsBackdrop) { state.settingsOpen = false; render(); }
  });
  els.settingsDialog.addEventListener("click", function (e) { e.stopPropagation(); });
  els.gateExitBtn.addEventListener("click", gateExit);
  els.logoutBtn.addEventListener("click", logout);
  els.pickPhotoBtn.addEventListener("click", function () { els.photoInput.click(); });
  els.clearPhotoBtn.addEventListener("click", function () { setProfile({ photo: "" }); });
  els.photoInput.addEventListener("change", onPhoto);
  els.pickBannerBtn.addEventListener("click", function () { els.bannerInput.click(); });
  els.clearBannerBtn.addEventListener("click", function () { setProfile({ banner: "" }); });
  els.bannerInput.addEventListener("change", onBanner);
  els.saveDescBtn.addEventListener("click", saveDesc);
  els.resetBtn.addEventListener("click", resetConversation);
  els.clearHistoryBtn.addEventListener("click", clearHistory);
  els.resetAllBtn.addEventListener("click", resetAll);

  els.profileCloseBtn.addEventListener("click", closeProfileCard);
  els.profileBackdrop.addEventListener("click", function (e) {
    if (e.target === els.profileBackdrop) closeProfileCard();
  });
  els.profileEditBtn.addEventListener("click", function () {
    closeProfileCard();
    openSettings();
  });

  els.draftInput.addEventListener("input", function (e) {
    state.draft = e.target.value;
    renderComposer();
    updateSlashUI();
  });
  els.draftInput.addEventListener("keydown", function (e) {
    if (state.slash.open && state.slash.filtered.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); state.slash.index = (state.slash.index + 1) % state.slash.filtered.length; renderSlashPopup(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); state.slash.index = (state.slash.index - 1 + state.slash.filtered.length) % state.slash.filtered.length; renderSlashPopup(); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); selectCommand(state.slash.filtered[state.slash.index]); return; }
      if (e.key === "Escape") { e.preventDefault(); closeSlashPopup(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  els.sendBtn.addEventListener("click", send);

  document.addEventListener("click", function (e) {
    if (state.slash.open && e.target !== els.draftInput && !els.slashPopup.contains(e.target)) closeSlashPopup();
    // infoPanel is a floating popover now (see index.html), not a docked
    // sidebar -- close it on an outside click, same as the slash popup.
    if (state.infoOpen && !els.infoPanel.contains(e.target) && e.target !== els.detailsBtn) {
      state.infoOpen = false;
      render();
    }
  });

  // ── init ──
  renderComposer();
  checkAuth();
})();
