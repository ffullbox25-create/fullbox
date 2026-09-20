/**
 * FullBox chat messenger helpers: polling fallback, sound, toast, title badge.
 * Works without build step. Prefer WebSocket when available.
 */
(function (global) {
  "use strict";

  var SOUND_KEY = "fb_chat_sound_unlocked";
  var lastSoundAt = 0;
  var audioCtx = null;

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function playPing() {
    if (localStorage.getItem(SOUND_KEY) !== "1") return;
    var now = Date.now();
    if (now - lastSoundAt < 3500) return;
    lastSoundAt = now;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audioCtx = audioCtx || new Ctx();
      var o = audioCtx.createOscillator();
      var g = audioCtx.createGain();
      o.type = "sine";
      o.frequency.value = 880;
      g.gain.value = 0.04;
      o.connect(g);
      g.connect(audioCtx.destination);
      o.start();
      setTimeout(function () {
        o.stop();
      }, 120);
    } catch (e) {}
  }

  function unlockSound() {
    localStorage.setItem(SOUND_KEY, "1");
    playPing();
  }

  function showToast(opts) {
    opts = opts || {};
    var host = document.getElementById("fb-chat-toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "fb-chat-toast-host";
      host.style.cssText =
        "position:fixed;top:16px;right:16px;z-index:9999;display:grid;gap:8px;max-width:360px;";
      document.body.appendChild(host);
    }
    var el = document.createElement("div");
    el.className = "fb-chat-toast";
    el.style.cssText =
      "background:#fffdf8;border:1px solid #e4ddd0;border-radius:14px;padding:12px 14px;" +
      "box-shadow:0 10px 28px rgba(28,31,26,.12);font:600 13px/1.35 Manrope,sans-serif;color:#1c1f1a;";
    el.innerHTML =
      "<div style='font-size:12px;color:#6b6f66;margin-bottom:4px'>" +
      escapeHtml(opts.title || "Новое сообщение") +
      "</div><div>" +
      escapeHtml(opts.text || "") +
      "</div>";
    if (opts.href) {
      el.style.cursor = "pointer";
      el.addEventListener("click", function () {
        location.href = opts.href;
      });
    }
    host.appendChild(el);
    var timer = setTimeout(function () {
      el.remove();
    }, 7000);
    el.addEventListener("mouseenter", function () {
      clearTimeout(timer);
    });
    el.addEventListener("mouseleave", function () {
      timer = setTimeout(function () {
        el.remove();
      }, 2000);
    });
    maybeBrowserNotify(opts);
  }

  function maybeBrowserNotify(opts) {
    opts = opts || {};
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    if (document.visibilityState === "visible") return;
    try {
      var n = new Notification(opts.title || "FullBox", {
        body: String(opts.text || "").slice(0, 140),
        tag: "fb-chat-" + String(opts.threadId || opts.href || "x"),
      });
      n.onclick = function () {
        window.focus();
        if (opts.href) location.href = opts.href;
        n.close();
      };
    } catch (e) {}
  }

  function requestBrowserNotifications() {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    if (Notification.permission === "granted") return Promise.resolve("granted");
    if (Notification.permission === "denied") return Promise.resolve("denied");
    return Notification.requestPermission();
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setTabBadge(count) {
    var base = document.title.replace(/^\(\d+\+?\)\s*/, "");
    if (count > 0) {
      document.title = "(" + (count > 99 ? "99+" : count) + ") " + base;
    } else {
      document.title = base;
    }
  }

  function createPoller(opts) {
    var lastId = opts.lastId || 0;
    var timer = null;
    var stopped = false;
    async function tick() {
      if (stopped) return;
      try {
        var url = opts.url;
        var sep = url.indexOf("?") >= 0 ? "&" : "?";
        var resp = await fetch(url + sep + "since_id=" + encodeURIComponent(lastId), {
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        var payload = await resp.json();
        if (payload && payload.ok) {
          var messages = (payload.data && payload.data.messages) || [];
          if (messages.length && typeof opts.onMessages === "function") {
            opts.onMessages(messages);
            lastId = Math.max.apply(
              null,
              messages.map(function (m) {
                return Number(m.id) || 0;
              }).concat([lastId])
            );
          }
          if (payload.data && typeof payload.data.unread_count === "number") {
            setTabBadge(payload.data.unread_count);
            if (typeof opts.onUnread === "function") opts.onUnread(payload.data.unread_count);
          }
        }
      } catch (e) {
        if (typeof opts.onOffline === "function") opts.onOffline();
      }
      if (!stopped) timer = setTimeout(tick, opts.intervalMs || 4000);
    }
    timer = setTimeout(tick, opts.intervalMs || 4000);
    return {
      stop: function () {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
      setLastId: function (id) {
        lastId = Math.max(lastId, Number(id) || 0);
      },
    };
  }

  function connectSocket(threadId, handlers) {
    handlers = handlers || {};
    if (!threadId || !global.WebSocket) return null;
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + "/ws/chat/" + threadId + "/";
    var ws;
    var closed = false;
    var retry = 0;
    function open() {
      if (closed) return;
      try {
        ws = new WebSocket(url);
      } catch (e) {
        return null;
      }
      ws.onopen = function () {
        retry = 0;
        if (handlers.onOnline) handlers.onOnline();
      };
      ws.onclose = function () {
        if (handlers.onOffline) handlers.onOffline();
        if (!closed) {
          retry += 1;
          setTimeout(open, Math.min(10000, 1000 * retry));
        }
      };
      ws.onmessage = function (ev) {
        try {
          var data = JSON.parse(ev.data);
          if (handlers.onEvent) handlers.onEvent(data);
        } catch (e) {}
      };
    }
    open();
    return {
      close: function () {
        closed = true;
        if (ws) ws.close();
      },
      sendTyping: function (active) {
        if (ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "typing", active: !!active }));
        }
      },
    };
  }

  var EMOJI = ["👍", "✅", "👀", "❤️", "🔥", "❗", "😊", "🙏", "📦", "🚚", "📎", "⚠️"];

  function bindEmojiPicker(button, textarea) {
    if (!button || !textarea) return;
    var panel = document.createElement("div");
    panel.hidden = true;
    panel.style.cssText =
      "position:absolute;bottom:48px;left:0;background:#fffdf8;border:1px solid #e4ddd0;" +
      "border-radius:12px;padding:8px;display:flex;flex-wrap:wrap;gap:6px;max-width:220px;z-index:5;" +
      "box-shadow:0 8px 20px rgba(0,0,0,.08)";
    EMOJI.forEach(function (e) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = e;
      b.style.cssText = "border:0;background:transparent;font-size:18px;cursor:pointer;padding:4px";
      b.addEventListener("click", function () {
        var start = textarea.selectionStart || textarea.value.length;
        var end = textarea.selectionEnd || start;
        textarea.value = textarea.value.slice(0, start) + e + textarea.value.slice(end);
        textarea.focus();
        panel.hidden = true;
      });
      panel.appendChild(b);
    });
    var wrap = button.parentElement || button;
    wrap.style.position = "relative";
    wrap.appendChild(panel);
    var hideTimer = null;
    function showPanel() {
      if (hideTimer) clearTimeout(hideTimer);
      panel.hidden = false;
    }
    function hidePanel() {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(function () {
        panel.hidden = true;
      }, 180);
    }
    button.addEventListener("mouseenter", showPanel);
    button.addEventListener("focus", showPanel);
    panel.addEventListener("mouseenter", showPanel);
    panel.addEventListener("mouseleave", hidePanel);
    wrap.addEventListener("mouseleave", hidePanel);
    button.addEventListener("blur", hidePanel);
    button.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (panel.hidden) showPanel();
      else hidePanel();
    });
  }

  function newIdempotencyKey() {
    return "k-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function deliveryMarks(status) {
    if (status === "read") return "✓✓";
    if (status === "delivered") return "✓✓";
    if (status === "sent") return "✓";
    if (status === "error") return "!";
    return "";
  }

  global.FullboxChat = {
    csrfToken: csrfToken,
    playPing: playPing,
    unlockSound: unlockSound,
    showToast: showToast,
    setTabBadge: setTabBadge,
    createPoller: createPoller,
    connectSocket: connectSocket,
    bindEmojiPicker: bindEmojiPicker,
    newIdempotencyKey: newIdempotencyKey,
    deliveryMarks: deliveryMarks,
    escapeHtml: escapeHtml,
    requestBrowserNotifications: requestBrowserNotifications,
    maybeBrowserNotify: maybeBrowserNotify,
  };
})(window);
