(function () {
  "use strict";

  const HUES = { bot: 289 };
  const SWATCH_HUES = [289, 250, 200, 150, 60, 30, 340];
  const PROFILE_KEY = "lappland.webui.profile.v2";
  const POLL_MS = 3000;

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

  const state = {
    draft: "", thinking: false, infoOpen: true, settingsOpen: false,
    active: "general",
    messages: {}, loaded: {}, lastIds: {}, seenIds: {},
    profile: { hue: null, photo: "" },
    botName: "Lappland", model: "…", mood: "…", lastProvider: null, connected: true,
    account: null,
    editingId: null, editDraft: "",
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
    memberList: document.getElementById("memberList"),
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
    draftInput: document.getElementById("draftInput"),
    sendBtn: document.getElementById("sendBtn"),
    charCount: document.getElementById("charCount"),
    accountBtn: document.getElementById("accountBtn"),
    settingsBackdrop: document.getElementById("settingsBackdrop"),
    settingsDialog: document.getElementById("settingsDialog"),
    settingsCloseX: document.getElementById("settingsCloseX"),
    settingsDoneBtn: document.getElementById("settingsDoneBtn"),
    accountLine: document.getElementById("accountLine"),
    logoutBtn: document.getElementById("logoutBtn"),
    bigAvatar: document.getElementById("bigAvatar"),
    pickPhotoBtn: document.getElementById("pickPhotoBtn"),
    clearPhotoBtn: document.getElementById("clearPhotoBtn"),
    photoInput: document.getElementById("photoInput"),
    swatchList: document.getElementById("swatchList"),
    storageNote: document.getElementById("storageNote"),
    clearHistoryBtn: document.getElementById("clearHistoryBtn"),
    resetAllBtn: document.getElementById("resetAllBtn"),
  };

  function activeChannel() {
    return CHANNELS.find(function (c) { return c.key === state.active; }) || CHANNELS[0];
  }

  // ── persistence (avatar photo/colour only -- local-only cosmetic, see
  // module docstring in webui_server.py for why identity itself lives
  // server-side now) ──
  function persist() {
    try { localStorage.setItem(PROFILE_KEY, JSON.stringify(state.profile)); } catch (e) { /* quota or blocked storage -- UI keeps working */ }
  }
  function loadPersisted() {
    try {
      const saved = JSON.parse(localStorage.getItem(PROFILE_KEY) || "null");
      if (saved) Object.assign(state.profile, saved);
    } catch (e) { /* ignore */ }
  }

  function setProfile(patch) {
    Object.assign(state.profile, patch);
    persist();
    render();
  }

  // ── auth ──
  function checkAuth() {
    fetch("/api/me")
      .then(function (res) { if (!res.ok) throw new Error("unauthenticated"); return res.json(); })
      .then(function (data) {
        state.account = { userId: data.user_id, username: data.username, isAdmin: !!data.isAdmin };
        document.body.classList.add("ready");
        refreshInfo();
        loadChannel(state.active);
        setInterval(pollActive, POLL_MS);
        render();
      })
      .catch(function () { window.location.href = "/login.html"; });
  }

  function logout() {
    fetch("/api/logout", { method: "POST" }).finally(function () { window.location.href = "/login.html"; });
  }

  // ── channel data ──
  function loadChannel(key) {
    fetch("/api/messages/" + encodeURIComponent(key))
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
    fetch("/api/messages/" + encodeURIComponent(key))
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
    fetch("/api/info")
      .then(function (res) { if (!res.ok) throw new Error("bad response"); return res.json(); })
      .then(function (data) {
        state.botName = data.botName || state.botName;
        state.model = data.model || state.model;
        state.mood = data.mood || state.mood;
        state.lastProvider = data.lastProvider || null;
        state.connected = true;
        render();
      })
      .catch(function () { state.connected = false; render(); });
  }

  // ── sending ──
  function send() {
    const text = state.draft.trim();
    if (!text) return;
    const chan = activeChannel();
    if (chan.botReplies && state.thinking) return;

    state.draft = "";
    state.thinking = chan.botReplies;
    els.draftInput.value = "";
    render();

    const active = state.active;
    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, channel: active }),
    })
      .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (result) {
        if (!result.ok) { state.thinking = false; state.connected = false; render(); return; }
        const data = result.data;
        const msgs = state.messages[active].slice();
        let lastId = state.lastIds[active] || 0;
        if (data.message) { msgs.push(data.message); lastId = data.message.id; }
        if (data.reply) { msgs.push(data.reply); lastId = data.reply.id; }
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
    return fetch("/api/reset", {
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
    try { localStorage.removeItem(PROFILE_KEY); } catch (e) {}
    if (state.account && state.account.isAdmin) clearHistory();
    state.profile = { hue: null, photo: "" };
    render();
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
    fetch("/api/messages/" + id + "/edit", {
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
    fetch("/api/messages/" + id + "/delete", { method: "POST" })
      .then(function (res) { if (!res.ok) throw new Error("failed"); return res.json(); })
      .then(function () {
        const key = state.active;
        state.messages[key] = state.messages[key].filter(function (m) { return m.id !== id; });
        render();
      })
      .catch(function () {});
  }

  // ── photo upload -- crop to a square, downscale, store as a data URL ──
  function onPhoto(e) {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = function () {
      const img = new Image();
      img.onload = function () {
        const n = 128;
        const cv = document.createElement("canvas");
        cv.width = n; cv.height = n;
        const ctx = cv.getContext("2d");
        const side = Math.min(img.width, img.height);
        ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, n, n);
        setProfile({ photo: cv.toDataURL("image/jpeg", 0.82) });
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(f);
    e.target.value = "";
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
    const members = [
      { name: state.botName, isBot: true, hue: HUES.bot, photo: "" },
      { name: myUsername || "…", isBot: false, hue: myHue, photo: state.profile.photo },
    ];
    els.memberList.innerHTML = "";
    members.forEach(function (m) {
      const row = h("div", { style: "display:flex;align-items:center;gap:9px;padding:5px 8px;border-radius:8px;" }, [
        h("div", { style: avatarStyle(m.hue, m.photo, 22), text: badge(m.name, m.photo) }),
        h("span", { style: "flex:1;font-size:13.5px;color:color-mix(in srgb, var(--color-text) 70%, transparent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", text: m.name }),
        m.isBot ? h("span", { class: "tag tag-accent", style: "font-size:9px;letter-spacing:0.06em;padding:2px 6px;", text: "BOT" }) : null,
      ]);
      els.memberList.appendChild(row);
    });

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
      const hue = m.bot ? HUES.bot : (isMe ? myHue : hueForName(m.author));
      const photo = isMe ? state.profile.photo : "";
      const editing = state.editingId === m.id;
      const isNew = !seen.has(m.id);

      const children = [];
      if (!grouped) {
        children.push(h("div", { style: "display:flex;align-items:baseline;gap:8px;margin-bottom:1px;" }, [
          h("span", { style: "font-family:var(--font-heading);font-weight:600;font-size:14px;color:oklch(0.82 0.05 " + hue + ");", text: m.author }),
          m.bot ? h("span", { class: "tag tag-accent", style: "font-size:9px;letter-spacing:0.06em;padding:2px 6px;", text: "BOT" }) : null,
          h("span", { style: "font-size:10.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);", text: formatTime(m.createdAt) }),
          m.edited ? h("span", { style: "font-size:10.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);font-style:italic;", text: "(edited)" }) : null,
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
        children.push(h("div", { style: "color:var(--color-text);white-space:pre-wrap;overflow-wrap:anywhere;text-wrap:pretty;", text: m.body }));
        if (m.image) {
          children.push(h("div", {
            role: "img", "aria-label": "generated image",
            style: "background-image:url(" + JSON.stringify(m.image) + ");background-size:contain;background-repeat:no-repeat;background-position:left center;background-color:var(--color-bg);width:100%;max-width:420px;height:420px;border-radius:10px;margin-top:8px;border:1px solid var(--color-divider);",
          }));
        }
        if (m.stats) {
          children.push(h("div", { style: "font-size:10.5px;color:color-mix(in srgb, var(--color-text) 40%, transparent);margin-top:5px;", text: m.stats }));
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

      const row = h("div", { class: "msg-row" + (isNew ? " msg-enter" : ""), style: "display:flex;gap:12px;padding:" + (grouped ? "1px" : "7px") + " 12px 2px 8px;border-radius:8px;" }, [
        h("div", { style: "width:38px;flex:none;padding-top:2px;" }, grouped ? null : h("div", { style: avatarStyle(hue, photo, 30), text: badge(m.author, photo) })),
        h("div", { style: "flex:1;min-width:0;" }, children),
        actions,
      ]);
      els.messageList.appendChild(row);
    });
    state.seenIds[state.active] = new Set(msgs.map(function (m) { return m.id; }));

    const showTyping = state.thinking && chan.botReplies;
    els.typingRow.style.display = showTyping ? "flex" : "none";
    if (showTyping) {
      els.typingAvatar.style.cssText = avatarStyle(HUES.bot, "", 30);
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
    els.aboutText.textContent = state.botName + " answers through " + state.model +
      ", falling back across Groq → Gemini → Cloudflare → Mistral → OpenRouter if a provider is rate-limited or down. " +
      "Type /help for text commands. #general is the only channel Lappland reads -- bug-reports, feature-suggestions and off-topic are just notes for you.";
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

    els.bigAvatar.style.cssText = avatarStyle(myHue, state.profile.photo, 60);
    els.bigAvatar.textContent = badge(myUsername, state.profile.photo);

    els.swatchList.innerHTML = "";
    SWATCH_HUES.forEach(function (hue) {
      els.swatchList.appendChild(h("button", {
        style: "width:28px;height:28px;border-radius:50%;cursor:pointer;background:oklch(0.5 0.09 " + hue + ");border:2px solid " + (state.profile.hue === hue ? "var(--color-neutral-100)" : "transparent") + ";",
        onclick: function () { setProfile({ hue: hue }); },
      }));
    });

    els.storageNote.textContent = isAdmin
      ? "Messages in every channel are shared and saved on the server -- everyone signed in sees the same conversation. Only your avatar photo/colour are kept in this browser. You're admin, so the buttons below wipe the shared channels for everyone."
      : "Messages in every channel are shared and saved on the server -- everyone signed in sees the same conversation. Only your avatar photo/colour are kept in this browser, and only you see them. Wiping shared channels is admin-only.";
    els.clearHistoryBtn.style.display = isAdmin ? "inline-flex" : "none";
  }

  function render() {
    renderSidebar();
    renderHeader();
    renderMessages();
    renderInfoPanel();
    renderComposer();
    renderSettings();
  }

  // ── shift-key tracking -- a class on <body> so a delete button's :hover
  // style can preview "this skips the confirmation" while Shift is held. ──
  document.addEventListener("keydown", function (e) { if (e.key === "Shift") document.body.classList.add("shift-down"); });
  document.addEventListener("keyup", function (e) { if (e.key === "Shift") document.body.classList.remove("shift-down"); });
  window.addEventListener("blur", function () { document.body.classList.remove("shift-down"); });

  // ── static event wiring (once) ──
  els.detailsBtn.addEventListener("click", function () { state.infoOpen = !state.infoOpen; render(); });
  els.accountBtn.addEventListener("click", function () { state.settingsOpen = true; render(); });
  els.settingsCloseX.addEventListener("click", function () { state.settingsOpen = false; render(); });
  els.settingsDoneBtn.addEventListener("click", function () { state.settingsOpen = false; render(); });
  els.settingsBackdrop.addEventListener("click", function (e) {
    if (e.target === els.settingsBackdrop) { state.settingsOpen = false; render(); }
  });
  els.settingsDialog.addEventListener("click", function (e) { e.stopPropagation(); });
  els.logoutBtn.addEventListener("click", logout);
  els.pickPhotoBtn.addEventListener("click", function () { els.photoInput.click(); });
  els.clearPhotoBtn.addEventListener("click", function () { setProfile({ photo: "" }); });
  els.photoInput.addEventListener("change", onPhoto);
  els.resetBtn.addEventListener("click", resetConversation);
  els.clearHistoryBtn.addEventListener("click", clearHistory);
  els.resetAllBtn.addEventListener("click", resetAll);

  els.draftInput.addEventListener("input", function (e) {
    state.draft = e.target.value;
    renderComposer();
  });
  els.draftInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  els.sendBtn.addEventListener("click", send);

  // ── init ──
  loadPersisted();
  renderComposer();
  checkAuth();
})();
