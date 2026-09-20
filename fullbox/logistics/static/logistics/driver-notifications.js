(function () {
  'use strict';

  const employeeId = String(document.body.dataset.driverEmployeeId || '').trim();
  if (!employeeId) return;

  const endpoint = '/logistics/driver/notifications/';
  const soundKey = 'fullbox-driver-sound-enabled';
  const lastIdKey = 'fullbox-driver-last-assignment-' + employeeId;
  const toggle = document.getElementById('driver-sound-toggle');
  const setup = document.getElementById('driver-sound-setup');
  const enableButton = document.getElementById('driver-sound-enable');
  const alertBox = document.getElementById('driver-assignment-alert');
  const alertTitle = document.getElementById('driver-assignment-title');
  const alertText = document.getElementById('driver-assignment-text');
  const alertOpen = document.getElementById('driver-assignment-open');
  const alertClose = document.getElementById('driver-assignment-close');
  let audioContext = null;
  let polling = false;

  function storageGet(key, fallback) {
    try { return window.localStorage.getItem(key) || fallback; } catch (_error) { return fallback; }
  }

  function storageSet(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (_error) { /* Private mode may block storage. */ }
  }

  function soundEnabled() {
    return storageGet(soundKey, '0') === '1';
  }

  function syncSoundUi() {
    const enabled = soundEnabled();
    if (toggle) {
      toggle.setAttribute('aria-pressed', enabled ? 'true' : 'false');
      toggle.textContent = enabled ? '🔔 Звук' : '🔕 Звук';
    }
    if (setup) setup.hidden = enabled;
  }

  function getAudioContext() {
    if (audioContext) return audioContext;
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return null;
    audioContext = new AudioContext();
    return audioContext;
  }

  async function unlockAudio() {
    const context = getAudioContext();
    if (context && context.state === 'suspended') {
      try { await context.resume(); } catch (_error) { /* The next user gesture can retry. */ }
    }
    return context;
  }

  async function playChime() {
    if (!soundEnabled()) return;
    const context = await unlockAudio();
    if (!context || context.state !== 'running') return;
    const start = context.currentTime;
    [659.25, 783.99, 987.77].forEach(function (frequency, index) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const noteStart = start + (index * 0.16);
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(frequency, noteStart);
      gain.gain.setValueAtTime(0.0001, noteStart);
      gain.gain.exponentialRampToValueAtTime(0.22, noteStart + 0.025);
      gain.gain.exponentialRampToValueAtTime(0.0001, noteStart + 0.28);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(noteStart);
      oscillator.stop(noteStart + 0.3);
    });
  }

  function speakAssignment(notification) {
    if (!soundEnabled() || !('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) return;
    try {
      window.speechSynthesis.cancel();
      const prefix = notification.kind === 'preliminary'
        ? 'Предварительно запланирован рейс'
        : 'Назначен новый рейс';
      const phrase = notification.trip_number ? prefix + ' ' + notification.trip_number : prefix;
      const utterance = new SpeechSynthesisUtterance(phrase);
      utterance.lang = 'ru-RU';
      utterance.rate = 0.92;
      utterance.pitch = 1;
      window.speechSynthesis.speak(utterance);
    } catch (_error) { /* Voice synthesis is optional. */ }
  }

  function showSystemNotification(notification) {
    if (!('Notification' in window) || window.Notification.permission !== 'granted') return;
    try {
      const systemNotification = new window.Notification(notification.title || 'Назначен новый рейс', {
        body: notification.text || '',
        tag: 'fullbox-trip-' + notification.id,
        renotify: true
      });
      systemNotification.onclick = function () {
        window.focus();
        window.location.assign(notification.url || '/logistics/driver/trips/');
        systemNotification.close();
      };
    } catch (_error) { /* Some mobile browsers only support service-worker notifications. */ }
  }

  function showAssignment(notification) {
    if (alertBox) {
      if (alertTitle) alertTitle.textContent = notification.title || 'Назначен новый рейс';
      if (alertText) alertText.textContent = notification.text || 'Откройте карточку рейса, чтобы увидеть маршрут и груз.';
      if (alertOpen) alertOpen.href = notification.url || '/logistics/driver/trips/';
      alertBox.hidden = false;
      window.requestAnimationFrame(function () { alertBox.classList.add('show'); });
    }
    playChime();
    window.setTimeout(function () { speakAssignment(notification); }, 480);
    if ('vibrate' in navigator) navigator.vibrate([180, 90, 180]);
    showSystemNotification(notification);
  }

  function closeAssignment() {
    if (!alertBox) return;
    alertBox.classList.remove('show');
    window.setTimeout(function () { alertBox.hidden = true; }, 220);
  }

  async function requestSystemPermission() {
    if (!('Notification' in window) || window.Notification.permission !== 'default') return;
    try { await window.Notification.requestPermission(); } catch (_error) { /* Optional enhancement. */ }
  }

  async function enableSound() {
    storageSet(soundKey, '1');
    syncSoundUi();
    await unlockAudio();
    await playChime();
    await requestSystemPermission();
  }

  function disableSound() {
    storageSet(soundKey, '0');
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    syncSoundUi();
  }

  async function pollAssignments() {
    if (polling || !navigator.onLine) return;
    polling = true;
    const afterId = Math.max(0, Number(storageGet(lastIdKey, '0')) || 0);
    try {
      const response = await fetch(endpoint + '?after=' + encodeURIComponent(afterId), {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      });
      if (!response.ok) return;
      const payload = await response.json();
      const notifications = Array.isArray(payload.notifications) ? payload.notifications : [];
      const lastId = Math.max(afterId, Number(payload.last_id || 0));
      if (lastId > afterId) storageSet(lastIdKey, lastId);
      if (notifications.length) showAssignment(notifications[notifications.length - 1]);
    } catch (_error) {
      /* The offline helper displays connectivity state. */
    } finally {
      polling = false;
    }
  }

  if (toggle) {
    toggle.addEventListener('click', function () {
      if (soundEnabled()) disableSound(); else enableSound();
    });
  }
  if (enableButton) enableButton.addEventListener('click', enableSound);
  if (alertClose) alertClose.addEventListener('click', closeAssignment);
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeAssignment(); });
  document.addEventListener('pointerdown', function () { if (soundEnabled()) unlockAudio(); }, { once: true });
  document.addEventListener('keydown', function () { if (soundEnabled()) unlockAudio(); }, { once: true });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) pollAssignments(); });
  window.addEventListener('focus', pollAssignments);
  window.addEventListener('online', pollAssignments);

  syncSoundUi();
  pollAssignments();
  window.setInterval(pollAssignments, 12000);
})();
