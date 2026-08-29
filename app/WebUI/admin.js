(function () {
  "use strict";

  // ── tiny DOM builder -- textContent for anything user-controlled, never
  // innerHTML, so usernames/emails/memory notes can't inject markup. Same
  // helper as chat.js. ──
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

  const els = {
    app: document.getElementById("app"),
    errorBanner: document.getElementById("errorBanner"),
    botAvatarPreview: document.getElementById("botAvatarPreview"),
    pickBotPhotoBtn: document.getElementById("pickBotPhotoBtn"),
    clearBotPhotoBtn: document.getElementById("clearBotPhotoBtn"),
    botPhotoInput: document.getElementById("botPhotoInput"),
    botBannerPreview: document.getElementById("botBannerPreview"),
    pickBotBannerBtn: document.getElementById("pickBotBannerBtn"),
    clearBotBannerBtn: document.getElementById("clearBotBannerBtn"),
    botBannerInput: document.getElementById("botBannerInput"),
    botBioInput: document.getElementById("botBioInput"),
    botBioBtn: document.getElementById("botBioBtn"),
    botAboutInput: document.getElementById("botAboutInput"),
    botAboutBtn: document.getElementById("botAboutBtn"),
    statusGrid: document.getElementById("statusGrid"),
    moodInput: document.getElementById("moodInput"),
    moodBtn: document.getElementById("moodBtn"),
    providerRows: document.getElementById("providerRows"),
    channelRows: document.getElementById("channelRows"),
    userRows: document.getElementById("userRows"),
    userCount: document.getElementById("userCount"),
    memoryRows: document.getElementById("memoryRows"),
    memoryEmpty: document.getElementById("memoryEmpty"),
    wipeAllMemoryBtn: document.getElementById("wipeAllMemoryBtn"),
    logoutLink: document.getElementById("logoutLink"),
  };

  // Same avatar-circle rendering as chat.js's avatarStyle/badge, just for
  // the one bot preview here -- not worth sharing a whole module for two
  // small functions used in exactly one place each.
  function botAvatarStyle(photo) {
    let css = "width:52px;height:52px;border-radius:50%;flex:none;display:grid;place-items:center;font-size:20px;font-weight:600;overflow:hidden;";
    if (photo) css += "background-image:url(" + JSON.stringify(photo) + ");background-size:cover;background-position:center;color:transparent;";
    else css += "background:oklch(0.45 0.07 289);color:oklch(0.96 0.02 289);";
    return css;
  }

  function showError(msg) {
    els.errorBanner.textContent = msg;
    els.errorBanner.style.display = "block";
  }

  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts))
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) throw new Error(data.detail || ("request failed (" + res.status + ")"));
          return data;
        });
      });
  }

  function fmtCooldown(seconds) {
    if (!seconds || seconds <= 0) return "—";
    if (seconds < 60) return Math.ceil(seconds) + "s";
    return Math.ceil(seconds / 60) + "m";
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
  }

  // ── overview: bot status + provider chain + channels ──
  function loadOverview() {
    return api("api/admin/overview").then(function (data) {
      els.statusGrid.innerHTML = "";
      [
        ["bot", data.botName], ["model", data.model], ["mood", data.mood],
        ["served by", data.lastProvider || "—"],
        ["forced provider", data.forcedProvider || "auto"],
        ["users", String(data.userCount)],
      ].forEach(function (pair) {
        els.statusGrid.appendChild(h("div", { class: "stat" }, [
          h("span", { class: "stat-label", text: pair[0] }),
          h("span", { class: "stat-value", text: pair[1] }),
        ]));
      });
      els.moodInput.value = data.mood || "";

      els.botAvatarPreview.style.cssText = botAvatarStyle(data.botAvatar);
      els.botAvatarPreview.textContent = data.botAvatar ? "" : (data.botName || "?").slice(0, 1).toUpperCase();
      els.botBannerPreview.style.backgroundImage = data.botBanner ? "url(" + JSON.stringify(data.botBanner) + ")" : "none";
      els.botBioInput.value = data.botBio || "";
      els.botAboutInput.value = data.botAbout || "";

      els.providerRows.innerHTML = "";
      Object.keys(data.providers).forEach(function (name) {
        const p = data.providers[name];
        const state = p.disabled ? "disabled" : (p.cooldownSeconds > 0 ? "cooling down" : "available");
        const stateColor = p.disabled ? "oklch(0.6 0.18 25)" : (p.cooldownSeconds > 0 ? "oklch(0.75 0.14 70)" : "oklch(0.72 0.14 145)");
        els.providerRows.appendChild(h("tr", null, [
          h("td", null, [
            h("span", { text: name }),
            p.forced ? h("span", { class: "tag tag-accent", style: "margin-left:8px;font-size:9px;padding:2px 6px;", text: "FORCED" }) : null,
          ]),
          h("td", null, h("span", { style: "color:" + stateColor + ";font-size:13px;", text: state })),
          h("td", { text: fmtCooldown(p.cooldownSeconds) }),
          h("td", null, p.forced
            ? h("button", { class: "btn btn-ghost row-btn", onclick: function () { setProvider("auto"); }, text: "Unpin" })
            : h("button", {
                class: "btn btn-secondary row-btn", disabled: p.disabled,
                onclick: function () { setProvider(name); }, text: "Force",
              })),
        ]));
      });

      els.channelRows.innerHTML = "";
      data.channels.forEach(function (c) {
        els.channelRows.appendChild(h("tr", null, [
          h("td", { text: "#" + c.key }),
          h("td", { text: String(c.count) }),
          h("td", null, h("button", {
            class: "btn btn-ghost row-btn", style: "color:oklch(0.72 0.15 25);",
            onclick: function () { resetChannel(c.key); }, text: "Reset",
          })),
        ]));
      });
    });
  }

  // ── bot profile (avatar + bio) ──
  function setBotAvatar(dataUrlOrEmpty) {
    api("api/admin/bot-profile", { method: "POST", body: JSON.stringify({ avatar: dataUrlOrEmpty }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function onBotPhoto(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    window.openImageCropper(f).then(function (dataUrl) {
      if (dataUrl) setBotAvatar(dataUrl);
    });
  }

  function setBotBanner(dataUrlOrEmpty) {
    api("api/admin/bot-profile", { method: "POST", body: JSON.stringify({ banner: dataUrlOrEmpty }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function onBotBanner(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    window.openImageCropper(f, { width: 320, height: 100, round: false, outputWidth: 960, outputHeight: 300 }).then(function (dataUrl) {
      if (dataUrl) setBotBanner(dataUrl);
    });
  }

  function saveBotBio() {
    api("api/admin/bot-profile", { method: "POST", body: JSON.stringify({ bio: els.botBioInput.value }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function saveBotAbout() {
    api("api/admin/bot-profile", { method: "POST", body: JSON.stringify({ about: els.botAboutInput.value }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function setMood() {
    const mood = els.moodInput.value.trim();
    if (!mood) return;
    api("api/admin/mood", { method: "POST", body: JSON.stringify({ mood: mood }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function setProvider(name) {
    api("api/admin/provider", { method: "POST", body: JSON.stringify({ provider: name }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  function resetChannel(key) {
    if (!window.confirm("Reset #" + key + "? This deletes every message in it for everyone.")) return;
    api("api/reset", { method: "POST", body: JSON.stringify({ channel: key }) })
      .then(loadOverview)
      .catch(function (e) { showError(e.message); });
  }

  // ── users ──
  let currentUserId = null;

  function loadUsers() {
    return api("api/admin/users").then(function (data) {
      els.userCount.textContent = data.users.length + " registered";
      els.userRows.innerHTML = "";
      data.users.forEach(function (u) {
        const canDelete = !u.isAdmin && u.userId !== currentUserId;
        els.userRows.appendChild(h("tr", null, [
          h("td", null, [
            h("span", { text: u.username }),
            u.isAdmin ? h("span", { class: "tag tag-accent", style: "margin-left:8px;font-size:9px;padding:2px 6px;", text: "ADMIN" }) : null,
          ]),
          h("td", { text: u.email || "—" }),
          h("td", { text: fmtDate(u.createdAt) }),
          h("td", null, canDelete ? h("button", {
            class: "btn btn-ghost row-btn", style: "color:oklch(0.72 0.15 25);",
            onclick: function () { deleteUser(u.userId, u.username); }, text: "Delete",
          }) : null),
        ]));
      });
    });
  }

  function deleteUser(userId, username) {
    if (!window.confirm("Delete " + username + "'s account? This can't be undone.")) return;
    api("api/admin/users/delete", { method: "POST", body: JSON.stringify({ user_id: userId }) })
      .then(loadUsers)
      .catch(function (e) { showError(e.message); });
  }

  // ── bot memory ──
  function loadMemory() {
    return api("api/admin/memory").then(function (data) {
      els.memoryRows.innerHTML = "";
      els.memoryEmpty.style.display = data.entries.length ? "none" : "block";
      data.entries.forEach(function (m) {
        els.memoryRows.appendChild(h("tr", null, [
          h("td", { style: "white-space:nowrap;", text: m.displayName }),
          h("td", { style: "max-width:360px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;opacity:0.85;", text: m.notes }),
          h("td", null, h("button", {
            class: "btn btn-ghost row-btn", style: "color:oklch(0.72 0.15 25);",
            onclick: function () { wipeMemory(m.userId, m.displayName); }, text: "Wipe",
          })),
        ]));
      });
    });
  }

  function wipeMemory(userId, displayName) {
    if (!window.confirm("Wipe what the bot remembers about " + displayName + "?")) return;
    api("api/admin/memory/wipe", { method: "POST", body: JSON.stringify({ user_id: userId }) })
      .then(loadMemory)
      .catch(function (e) { showError(e.message); });
  }

  function wipeAllMemory() {
    if (!window.confirm("Wipe ALL bot memory for every user? This can't be undone.")) return;
    api("api/admin/memory/wipe-all", { method: "POST" })
      .then(loadMemory)
      .catch(function (e) { showError(e.message); });
  }

  // ── auth gate ──
  function checkAuth() {
    fetch("api/me")
      .then(function (res) { if (!res.ok) throw new Error("unauthenticated"); return res.json(); })
      .then(function (data) {
        if (!data.isAdmin) { window.location.href = "./"; return; }
        currentUserId = data.user_id;
        els.app.style.display = "block";
        Promise.all([loadOverview(), loadUsers(), loadMemory()])
          .catch(function (e) { showError(e.message); });
      })
      .catch(function () { window.location.href = "login.html"; });
  }

  els.moodBtn.addEventListener("click", setMood);
  els.moodInput.addEventListener("keydown", function (e) { if (e.key === "Enter") setMood(); });
  els.pickBotPhotoBtn.addEventListener("click", function () { els.botPhotoInput.click(); });
  els.clearBotPhotoBtn.addEventListener("click", function () { setBotAvatar(""); });
  els.botPhotoInput.addEventListener("change", onBotPhoto);
  els.pickBotBannerBtn.addEventListener("click", function () { els.botBannerInput.click(); });
  els.clearBotBannerBtn.addEventListener("click", function () { setBotBanner(""); });
  els.botBannerInput.addEventListener("change", onBotBanner);
  els.botBioBtn.addEventListener("click", saveBotBio);
  els.botAboutBtn.addEventListener("click", saveBotAbout);
  els.wipeAllMemoryBtn.addEventListener("click", wipeAllMemory);
  els.logoutLink.addEventListener("click", function (e) {
    e.preventDefault();
    fetch("api/logout", { method: "POST" }).finally(function () { window.location.href = "login.html"; });
  });

  checkAuth();
})();
