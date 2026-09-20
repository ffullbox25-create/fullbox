/**
 * Вложения чата: превью, drag-and-drop, lightbox для изображений.
 */
(function (global) {
  "use strict";

  var FB = global.FullboxChat || (global.FullboxChat = {});

  function escapeHtml(s) {
    if (FB.escapeHtml) return FB.escapeHtml(s);
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ensureLightbox() {
    var box = document.getElementById("fb-chat-lightbox");
    if (box) return box;
    box = document.createElement("div");
    box.id = "fb-chat-lightbox";
    box.hidden = true;
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML =
      '<div class="fb-lb-backdrop" data-lb-close="1"></div>' +
      '<figure class="fb-lb-figure">' +
      '  <button type="button" class="fb-lb-close" data-lb-close="1" aria-label="Закрыть">×</button>' +
      '  <img class="fb-lb-img" alt="">' +
      '  <figcaption class="fb-lb-cap"></figcaption>' +
      '  <a class="fb-lb-download" href="#" target="_blank" rel="noopener">Скачать</a>' +
      "</figure>";
    var style = document.createElement("style");
    style.textContent =
      "#fb-chat-lightbox{position:fixed;inset:0;z-index:10050;display:grid;place-items:center;}" +
      "#fb-chat-lightbox[hidden]{display:none!important;}" +
      "#fb-chat-lightbox .fb-lb-backdrop{position:absolute;inset:0;background:rgba(28,31,26,.72);}" +
      "#fb-chat-lightbox .fb-lb-figure{position:relative;z-index:1;margin:0;max-width:min(92vw,1100px);max-height:90vh;" +
      "display:grid;gap:10px;justify-items:center;}" +
      "#fb-chat-lightbox .fb-lb-img{max-width:92vw;max-height:78vh;border-radius:12px;box-shadow:0 20px 50px rgba(0,0,0,.35);" +
      "background:#fffdf8;}" +
      "#fb-chat-lightbox .fb-lb-cap{color:#fffdf8;font:600 13px/1.35 Manrope,sans-serif;text-align:center;}" +
      "#fb-chat-lightbox .fb-lb-download{color:#f2b747;font:700 13px Manrope,sans-serif;text-decoration:none;}" +
      "#fb-chat-lightbox .fb-lb-close{position:absolute;top:-8px;right:-8px;width:36px;height:36px;border:0;border-radius:999px;" +
      "background:#fffdf8;color:#1c1f1a;font-size:22px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2);}";
    document.head.appendChild(style);
    document.body.appendChild(box);
    box.addEventListener("click", function (ev) {
      if (ev.target && ev.target.getAttribute("data-lb-close")) closeLightbox();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !box.hidden) closeLightbox();
    });
    return box;
  }

  function openLightbox(url, title) {
    var box = ensureLightbox();
    var img = box.querySelector(".fb-lb-img");
    var cap = box.querySelector(".fb-lb-cap");
    var dl = box.querySelector(".fb-lb-download");
    var src = String(url || "");
    if (src.indexOf("?") >= 0) src += "&inline=1";
    else src += "?inline=1";
    img.src = src;
    img.alt = title || "Изображение";
    cap.textContent = title || "";
    dl.href = url || "#";
    dl.download = title || "";
    box.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeLightbox() {
    var box = document.getElementById("fb-chat-lightbox");
    if (!box) return;
    box.hidden = true;
    var img = box.querySelector(".fb-lb-img");
    if (img) img.removeAttribute("src");
    document.body.style.overflow = "";
  }

  function renderAttachmentsHtml(files) {
    var rows = Array.isArray(files) ? files : [];
    if (!rows.length) return "";
    return (
      '<div class="msg-files chat-files">' +
      rows
        .map(function (f) {
          var url = f.url || "#";
          var name = f.filename || "файл";
          var meta = f.size_label || "";
          if (f.is_image) {
            return (
              '<a class="chat-file-thumb" href="' +
              escapeHtml(url) +
              '" data-lightbox="1" data-filename="' +
              escapeHtml(name) +
              '">' +
              '<img src="' +
              escapeHtml(url + (url.indexOf("?") >= 0 ? "&" : "?") + "inline=1") +
              '" alt="' +
              escapeHtml(name) +
              '" loading="lazy">' +
              "<span>" +
              escapeHtml(name) +
              (meta ? " · " + escapeHtml(meta) : "") +
              "</span></a>"
            );
          }
          return (
            '<a class="chat-file-link" href="' +
            escapeHtml(url) +
            '" target="_blank" rel="noopener">' +
            "📎 " +
            escapeHtml(name) +
            (meta ? " <small>(" + escapeHtml(meta) + ")</small>" : "") +
            "</a>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function bindLightbox(root) {
    var el = root || document;
    el.addEventListener("click", function (ev) {
      var a = ev.target.closest("[data-lightbox]");
      if (!a) return;
      ev.preventDefault();
      openLightbox(a.getAttribute("href"), a.getAttribute("data-filename") || a.textContent);
    });
  }

  function bindComposerDropzone(composer, fileInput, previewEl) {
    if (!composer || !fileInput) return;
    var zone = composer.querySelector(".chat-dropzone") || composer;
    var preview = previewEl || composer.querySelector(".chat-attach-preview");

    function syncPreview() {
      if (!preview) return;
      var list = fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
      if (!list.length) {
        preview.hidden = true;
        preview.innerHTML = "";
        return;
      }
      preview.hidden = false;
      preview.innerHTML = list
        .map(function (f) {
          return (
            "<span class='chat-attach-chip'>" +
            escapeHtml(f.name) +
            " <small>" +
            escapeHtml((f.size / 1024).toFixed(0) + " КБ") +
            "</small></span>"
          );
        })
        .join("");
    }

    function mergeFiles(fileList) {
      var dt = new DataTransfer();
      var existing = fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
      existing.concat(Array.prototype.slice.call(fileList || [])).forEach(function (f) {
        if (f) dt.items.add(f);
      });
      fileInput.files = dt.files;
      syncPreview();
    }

    fileInput.addEventListener("change", syncPreview);

    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        zone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (evt === "drop") {
          var files = ev.dataTransfer && ev.dataTransfer.files;
          if (files && files.length) mergeFiles(files);
        }
        zone.classList.remove("is-dragover");
      });
    });

    syncPreview();
  }

  FB.renderAttachmentsHtml = renderAttachmentsHtml;
  FB.bindLightbox = bindLightbox;
  FB.bindComposerDropzone = bindComposerDropzone;
  FB.openLightbox = openLightbox;
  FB.closeLightbox = closeLightbox;
})(window);
