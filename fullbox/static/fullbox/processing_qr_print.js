(() => {
  const PRINT_JOB_URL = '/orders/processing/print-jobs/';
  const PRINTER_KEYS = ['processing_label_printer', 'label_printer'];
  const AGENT_KEYS = ['labels_print_agent_id', 'fullbox_agent_id'];
  const VIRTUAL_PRINTER_MARKERS = ['pdf', 'xps', 'onenote', 'fax', 'microsoft print', 'save to pdf', 'anydesk', 'rustdesk', 'remote desktop', 'redirected'];
  let lastBoxTarget = null;
  let lastPalletTarget = null;
  let rendererFrame = null;
  let rendererReady = false;
  const pending = new Map();

  const getOrderId = () => {
    const match = window.location.pathname.match(/\/orders\/processing\/([^/]+)\/flow\/?/);
    return match ? decodeURIComponent(match[1]) : '';
  };

  const getCsrfToken = () => {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : '';
  };

  const getJsonScript = (id, fallback) => {
    const el = document.getElementById(id);
    if (!el) {
      return fallback;
    }
    try {
      return JSON.parse(el.textContent || 'null');
    } catch (_error) {
      return fallback;
    }
  };

  const getClientLabel = () => String(getJsonScript('processing-client-label-data', '') || '').trim();
  const getMenuValue = (menuId, key) => {
    const menu = document.getElementById(menuId);
    return String(menu && menu.dataset ? menu.dataset[key] || '' : '').trim();
  };
  const getMenuJsonArray = (menuId, key) => {
    const raw = getMenuValue(menuId, key);
    if (!raw) {
      return [];
    }
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map((value) => String(value || '').trim()).filter(Boolean) : [];
    } catch (_error) {
      return [];
    }
  };

  const getStoredPrinter = () => {
    for (const key of PRINTER_KEYS) {
      const value = String(localStorage.getItem(key) || '').trim();
      if (value) {
        return value;
      }
    }
    return '';
  };

  const rememberAgentId = (agentId) => {
    const value = String(agentId || '').trim();
    if (!value) {
      return;
    }
    AGENT_KEYS.forEach((key) => localStorage.setItem(key, value));
  };

  const getStoredAgentId = () => {
    for (const key of AGENT_KEYS) {
      const value = String(localStorage.getItem(key) || '').trim();
      if (value) {
        return value;
      }
    }
    return '';
  };

  const hasPrintablePrinters = (payload) => {
    const printers = Array.isArray(payload && payload.printers) ? payload.printers : [];
    return printers.some((name) => {
      const key = String(name || '').trim().toLowerCase();
      return key && !VIRTUAL_PRINTER_MARKERS.some((marker) => key.includes(marker));
    });
  };

  const detectLocalAgent = async () => {
    try {
      const response = await fetch('http://127.0.0.1:17841/whoami', { cache: 'no-store' });
      if (!response.ok) {
        return null;
      }
      return await response.json();
    } catch (_error) {
      return null;
    }
  };

  const getAgentId = async () => {
    const detected = await detectLocalAgent();
    const detectedId = String(detected && detected.agentId ? detected.agentId : '').trim();
    if (detectedId && hasPrintablePrinters(detected)) {
      rememberAgentId(detectedId);
      return detectedId;
    }
    return getStoredAgentId() || detectedId;
  };

  const setStatus = (message, isError = false) => {
    const target = document.querySelector('[data-agent-status], #agent-status, #scanner-status');
    if (target) {
      target.textContent = message;
      target.classList.toggle('error', Boolean(isError));
    }
  };

  const resetRenderer = () => {
    rendererReady = false;
    if (!rendererFrame) {
      return;
    }
    try {
      rendererFrame.remove();
    } catch (_error) {
      if (rendererFrame.parentNode) {
        rendererFrame.parentNode.removeChild(rendererFrame);
      }
    }
    rendererFrame = null;
  };

  const ensureRenderer = () => new Promise((resolve, reject) => {
    if (rendererFrame && rendererReady) {
      resolve(rendererFrame);
      return;
    }
    if (rendererFrame && !rendererReady) {
      resetRenderer();
    }
    if (!rendererFrame) {
      const orderId = getOrderId();
      if (!orderId) {
        reject(new Error('order_id_missing'));
        return;
      }
      rendererReady = false;
      rendererFrame = document.createElement('iframe');
      rendererFrame.src = `/orders/processing/${encodeURIComponent(orderId)}/flow/label-render/`;
      rendererFrame.setAttribute('aria-hidden', 'true');
      rendererFrame.style.position = 'fixed';
      rendererFrame.style.left = '-10000px';
      rendererFrame.style.top = '0';
      rendererFrame.style.width = '80mm';
      rendererFrame.style.height = '90mm';
      rendererFrame.style.border = '0';
      rendererFrame.style.pointerEvents = 'none';
      rendererFrame.addEventListener('error', () => {
        resetRenderer();
        reject(new Error('renderer_load_failed'));
      }, { once: true });
      document.body.appendChild(rendererFrame);
    }
    const started = Date.now();
    const tick = () => {
      if (rendererReady) {
        resolve(rendererFrame);
        return;
      }
      if (Date.now() - started > 15000) {
        resetRenderer();
        reject(new Error('renderer_timeout'));
        return;
      }
      setTimeout(tick, 50);
    };
    tick();
  });

  window.addEventListener('message', (event) => {
    if (event.origin !== window.location.origin) {
      return;
    }
    const message = event.data || {};
    if (message.type === 'fullbox:processing-label-renderer-ready') {
      rendererReady = true;
      return;
    }
    if (message.type !== 'fullbox:render-processing-label-result') {
      return;
    }
    const requestId = String(message.requestId || '');
    const entry = pending.get(requestId);
    if (!entry) {
      return;
    }
    pending.delete(requestId);
    clearTimeout(entry.timer);
    if (message.ok && message.image) {
      entry.resolve(message.image);
    } else {
      entry.reject(new Error(message.error || 'render_failed'));
    }
  });

  const renderLabel = async (payload) => {
    const frame = await ensureRenderer();
    const requestId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error('render_timeout'));
      }, 20000);
      pending.set(requestId, { resolve, reject, timer });
      frame.contentWindow.postMessage({ type: 'fullbox:render-processing-label', requestId, payload }, window.location.origin);
    });
  };

  const readPrintQueueError = async (response) => {
    const fallback = `Ошибка очереди печати (HTTP ${response.status || '?'})`;
    let text = '';
    try {
      text = String(await response.text() || '').trim();
    } catch (_error) {
      return fallback;
    }
    if (!text) {
      return fallback;
    }
    try {
      const payload = JSON.parse(text);
      const message = String(
        payload && (payload.error || payload.message || payload.detail)
          ? (payload.error || payload.message || payload.detail)
          : ''
      ).trim();
      return message || fallback;
    } catch (_error) {
      return text;
    }
  };

  const postPrintJob = async (payload, image) => {
    const printer = getStoredPrinter();
    if (!printer) {
      throw new Error('Select label printer in settings.');
    }
    const agentId = await getAgentId();
    if (!agentId) {
      throw new Error('Select print agent in settings.');
    }
    const base64 = String(image || '').split(',')[1] || '';
    if (!base64) {
      throw new Error('label_image_empty');
    }
    const response = await fetch(PRINT_JOB_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
      },
      credentials: 'same-origin',
      body: JSON.stringify({
        order_id: getOrderId(),
        card_id: payload.kind === 'pallet' ? 'pallet' : 'box',
        article: payload.kind === 'pallet' ? 'pallet_qr' : 'box_qr',
        barcode: payload.code,
        size: payload.number || '',
        copies: 1,
        printer_name: printer,
        agent_id: agentId,
        label_png_base64: base64,
        label_width_mm: 58,
        label_height_mm: 60,
        template_key: payload.kind === 'pallet' ? 'pallet' : 'box',
      }),
    });
    if (!response.ok) {
      throw new Error(await readPrintQueueError(response));
    }
  };

  const queueLabels = async (labels) => {
    const list = (Array.isArray(labels) ? labels : [labels]).filter((item) => item && item.code);
    if (!list.length) {
      return;
    }
    for (let index = 0; index < list.length; index += 1) {
      const payload = { ...list[index], clientLabel: getClientLabel() };
      setStatus(list.length > 1 ? `Preparing QR: ${index + 1} / ${list.length}` : 'Preparing QR...', false);
      const image = await renderLabel(payload);
      await postPrintJob(payload, image);
    }
    setStatus(`QR sent to print: ${list.length}`, false);
  };
  const printLabels = (labels) => {
    const list = (Array.isArray(labels) ? labels : [labels]).filter((item) => item && item.code);
    if (!list.length) {
      const message = 'Не удалось определить цель печати. Откройте меню еще раз и повторите.';
      setStatus(message, true);
      alert(message);
      return Promise.resolve(false);
    }
    return queueLabels(list)
      .then(() => true)
      .catch((error) => {
        const message = String(error && error.message ? error.message : error || 'QR print error');
        setStatus(message, true);
        alert(message);
        return false;
      });
  };
  const closeMenu = (menu) => {
    if (!menu) {
      return;
    }
    menu.classList.remove('is-open');
    menu.setAttribute('aria-hidden', 'true');
  };

  const boxNumberForElement = (boxEl) => {
    const palletEl = boxEl ? boxEl.closest('[data-pallet-code]') : null;
    if (!palletEl) {
      return '';
    }
    const boxes = Array.from(palletEl.querySelectorAll('[data-box-code]'));
    const index = boxes.findIndex((item) => item.dataset.boxCode === boxEl.dataset.boxCode);
    return index >= 0 ? String(index + 1) : '';
  };

  const labelForBoxElement = (boxEl) => {
    const code = String(boxEl && boxEl.dataset ? boxEl.dataset.boxCode || '' : '').trim();
    return code ? { kind: 'box', code, number: boxNumberForElement(boxEl) } : null;
  };
  const labelForBoxCode = (code) => {
    const normalizedCode = String(code || '').trim();
    if (!normalizedCode) {
      return null;
    }
    const boxEl = findBoxElementByCode(normalizedCode);
    return { kind: 'box', code: normalizedCode, number: boxEl ? boxNumberForElement(boxEl) : '' };
  };

  const findBoxElementByCode = (code) => {
    const safeCode = String(code || '').replace(/"/g, '\\"');
    return safeCode ? document.querySelector(`[data-box-code="${safeCode}"]`) : null;
  };
  const findPalletElementByCode = (code) => {
    const safeCode = String(code || '').replace(/"/g, '\\"');
    return safeCode ? document.querySelector(`[data-pallet-code="${safeCode}"]`) : null;
  };

  const currentBoxLabel = () => {
    const codeEl = document.getElementById('current-box-code');
    const code = String(codeEl ? codeEl.textContent || '' : '').trim();
    if (!code || code === '-') {
      return null;
    }
    const boxEl = findBoxElementByCode(code);
    return { kind: 'box', code, number: boxEl ? boxNumberForElement(boxEl) : '' };
  };

  const labelsForPalletFromBox = (boxEl) => {
    const palletEl = boxEl ? boxEl.closest('[data-pallet-code]') : null;
    if (!palletEl) {
      return [];
    }
    return Array.from(palletEl.querySelectorAll('[data-box-code]'))
      .map((item, index) => ({ kind: 'box', code: String(item.dataset.boxCode || '').trim(), number: String(index + 1) }))
      .filter((item) => item.code);
  };
  const labelsForPalletCode = (palletCode, fallbackCodes = []) => {
    const palletEl = findPalletElementByCode(palletCode);
    if (palletEl) {
      const fromDom = Array.from(palletEl.querySelectorAll('[data-box-code]'))
        .map((item, index) => ({ kind: 'box', code: String(item.dataset.boxCode || '').trim(), number: String(index + 1) }))
        .filter((item) => item.code);
      if (fromDom.length) {
        return fromDom;
      }
    }
    return (Array.isArray(fallbackCodes) ? fallbackCodes : [])
      .map((code, index) => ({ kind: 'box', code: String(code || '').trim(), number: String(index + 1) }))
      .filter((item) => item.code);
  };

  const palletLabelForElement = (palletEl) => {
    const code = String(palletEl && palletEl.dataset ? palletEl.dataset.palletCode || '' : '').trim();
    if (!code) {
      return null;
    }
    const pallets = Array.from(document.querySelectorAll('[data-pallet-code]'));
    const index = pallets.findIndex((item) => item.dataset.palletCode === code);
    return { kind: 'pallet', code, number: index >= 0 ? String(index + 1) : '' };
  };
  const palletLabelForCode = (code) => {
    const normalizedCode = String(code || '').trim();
    if (!normalizedCode) {
      return null;
    }
    const palletEl = findPalletElementByCode(normalizedCode);
    return palletEl ? palletLabelForElement(palletEl) : { kind: 'pallet', code: normalizedCode, number: '' };
  };
  window.fullboxProcessingQrPrint = {
    printLabels,
    labelForBoxCode,
    labelsForPalletCode,
    palletLabelForCode,
  };

  const isCurrentBoxPrintButton = (button) => {
    const actions = button.closest('#current-box-items .box-actions, #current-box-items td, #current-box-items div');
    if (!actions || !button.closest('#current-box-items')) {
      return false;
    }
    const buttons = Array.from(actions.querySelectorAll('button'));
    return buttons.length > 0 && buttons[buttons.length - 1] === button;
  };

  document.addEventListener('contextmenu', (event) => {
    const box = event.target.closest('[data-box-code]');
    if (box) {
      lastBoxTarget = box;
    }
    const pallet = event.target.closest('[data-pallet-code]');
    if (pallet && !box) {
      lastPalletTarget = pallet;
    }
  }, true);
  const boxContextMenu = document.getElementById('box-context-menu');
  if (boxContextMenu) {
    boxContextMenu.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) {
        return;
      }
      const action = String(button.dataset.action || '').trim();
      if (action !== 'print-box' && action !== 'print-pallet') {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      const boxCode = String(boxContextMenu.dataset.boxCode || '').trim();
      const palletCode = String(boxContextMenu.dataset.palletCode || '').trim();
      const palletBoxes = getMenuJsonArray('box-context-menu', 'palletBoxes');
      closeMenu(boxContextMenu);
      if (action === 'print-box') {
        printLabels([labelForBoxCode(boxCode)]);
        return;
      }
      printLabels(labelsForPalletCode(palletCode, palletBoxes));
    }, true);
  }
  const palletContextMenu = document.getElementById('pallet-context-menu');
  if (palletContextMenu) {
    palletContextMenu.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) {
        return;
      }
      const action = String(button.dataset.action || '').trim();
      if (action !== 'print') {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      const palletCode = String(palletContextMenu.dataset.palletCode || '').trim();
      closeMenu(palletContextMenu);
      printLabels([palletLabelForCode(palletCode)]);
    }, true);
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) {
      return;
    }
    if (button.closest('#box-context-menu') || button.closest('#pallet-context-menu')) {
      return;
    }
    let labels = null;
    const action = button.dataset ? button.dataset.action || '' : '';
    if (isCurrentBoxPrintButton(button)) {
      labels = [currentBoxLabel()].filter(Boolean);
    }
    if (!labels) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    printLabels(labels);
  }, true);
})();
