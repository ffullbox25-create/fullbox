(function () {
  'use strict';

  const DB_NAME = 'fullbox-driver-outbox';
  const STORE_NAME = 'requests';
  const VERSION = 1;
  const banner = document.getElementById('offline-banner');
  const pendingNote = document.getElementById('pending-note');

  function setConnectionState() {
    if (banner) banner.classList.toggle('show', !navigator.onLine);
  }

  function openDb() {
    return new Promise(function (resolve, reject) {
      const request = indexedDB.open(DB_NAME, VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function transaction(mode, callback) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        const tx = db.transaction(STORE_NAME, mode);
        const store = tx.objectStore(STORE_NAME);
        callback(store, resolve, reject);
        tx.oncomplete = function () { db.close(); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function putQueued(item) {
    return transaction('readwrite', function (store, resolve, reject) {
      const request = store.put(item);
      request.onsuccess = function () { resolve(item); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function deleteQueued(id) {
    return transaction('readwrite', function (store, resolve, reject) {
      const request = store.delete(id);
      request.onsuccess = function () { resolve(); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function listQueued() {
    return transaction('readonly', function (store, resolve, reject) {
      const request = store.getAll();
      request.onsuccess = function () { resolve(request.result || []); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function cookie(name) {
    const prefix = name + '=';
    for (const part of document.cookie.split(';')) {
      const value = part.trim();
      if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
    }
    return '';
  }

  function ensureSubmissionId(form) {
    const field = form.querySelector('[name="submission_id"]');
    if (!field || field.value) return;
    field.value = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('driver-' + Date.now() + '-' + Math.random().toString(16).slice(2));
  }

  function serialize(form) {
    ensureSubmissionId(form);
    const entries = [];
    const data = new FormData(form);
    data.forEach(function (value, key) {
      if (key !== 'csrfmiddlewaretoken') entries.push([key, value]);
    });
    return entries;
  }

  function formDataFrom(entries) {
    const data = new FormData();
    data.append('csrfmiddlewaretoken', cookie('csrftoken'));
    entries.forEach(function (entry) { data.append(entry[0], entry[1]); });
    return data;
  }

  function formUrl(form) {
    return form.getAttribute('action') || window.location.href;
  }

  async function send(item) {
    const response = await fetch(item.url, {
      method: 'POST',
      body: formDataFrom(item.entries),
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    });
    let payload = {};
    try { payload = await response.json(); } catch (_error) { payload = {}; }
    if (!response.ok || !payload.ok) {
      const error = new Error(payload.message || 'Сервер отклонил данные. Проверьте форму.');
      error.serverRejected = true;
      throw error;
    }
    return payload;
  }

  async function queue(form) {
    const item = {
      id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('queued-' + Date.now() + '-' + Math.random().toString(16).slice(2)),
      url: formUrl(form),
      entries: serialize(form),
      createdAt: new Date().toISOString()
    };
    await putQueued(item);
    if (pendingNote) pendingNote.classList.add('show');
    form.dataset.pending = '1';
    const button = form.querySelector('[type="submit"]');
    if (button) { button.disabled = true; button.textContent = 'Ожидает отправки'; }
  }

  async function flush() {
    if (!navigator.onLine) return;
    const items = await listQueued();
    if (pendingNote) pendingNote.classList.toggle('show', items.length > 0);
    for (const item of items) {
      try {
        const payload = await send(item);
        await deleteQueued(item.id);
        if (payload.redirect_url && payload.redirect_url === window.location.pathname) {
          window.location.reload();
          return;
        }
      } catch (error) {
        if (error.serverRejected) {
          if (pendingNote) {
            pendingNote.classList.add('show');
            pendingNote.textContent = error.message + ' Данные сохранены на устройстве.';
          }
          return;
        }
        return;
      }
    }
    const remaining = await listQueued();
    if (pendingNote) pendingNote.classList.toggle('show', remaining.length > 0);
  }

  document.querySelectorAll('form[data-offline-form]').forEach(function (form) {
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!form.reportValidity()) return;
      ensureSubmissionId(form);
      const button = form.querySelector('[type="submit"]');
      const oldText = button ? button.textContent : '';
      if (button) { button.disabled = true; button.textContent = navigator.onLine ? 'Сохраняем…' : 'Ожидает отправки'; }
      if (!navigator.onLine) {
        await queue(form);
        return;
      }
      try {
        const payload = await send({ url: formUrl(form), entries: serialize(form) });
        if (payload.redirect_url) window.location.assign(payload.redirect_url);
      } catch (error) {
        if (error.serverRejected) {
          window.alert(error.message);
          if (button) { button.disabled = false; button.textContent = oldText; }
          return;
        }
        await queue(form);
      }
    });
  });

  window.addEventListener('online', function () { setConnectionState(); flush(); });
  window.addEventListener('offline', setConnectionState);
  setConnectionState();
  listQueued().then(function (items) { if (pendingNote) pendingNote.classList.toggle('show', items.length > 0); });
  if (navigator.onLine) flush();
})();
