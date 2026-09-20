(() => {
  const app = document.getElementById('receiving-tsd-app');
  if (!app) return;

  const parseJson = (id, fallback) => {
    try { return JSON.parse(document.getElementById(id)?.textContent || JSON.stringify(fallback)); }
    catch (_) { return fallback; }
  };
  let state = parseJson('receiving-tsd-state', {});
  let summary = parseJson('receiving-tsd-summary', {});
  let receivingLocation = parseJson('receiving-tsd-location', {});
  let pendingPlacementPallets = parseJson('receiving-tsd-pending-pallets', []);
  let placementPalletCode = '';
  let putawayPreview = parseJson('receiving-tsd-putaway', []);
  let agents = parseJson('receiving-tsd-agents', []);
  let pendingBarcode = '';
  let busy = false;
  let preferredScanTarget = 'product';
  let editingBox = false;
  let editingBoxCode = '';
  const isMarked = app.dataset.isMarked === '1';
  const editBoxButton = document.getElementById('edit-box');
  const correctionPanel = document.getElementById('box-correction-panel');
  const correctionStatus = document.getElementById('box-correction-status');
  const correctionMark = document.getElementById('correction-mark');
  const removeCorrectionMark = document.getElementById('remove-correction-mark');
  const finishCorrection = document.getElementById('finish-box-correction');

  const input = document.getElementById('scan-input');
  const quantityInput = document.getElementById('scan-quantity');
  const status = document.getElementById('scan-status');
  const locationInput = document.getElementById('location-input');
  const locationPallet = document.getElementById('location-pallet');
  locationPallet?.addEventListener('change', () => {
    placementPalletCode = locationPallet.value;
    locationInput.value = '';
    preferredScanTarget = 'location';
    setStatus(placementPalletCode ? `Отсканируйте место PR для палеты ${placementPalletCode}` : 'Выберите палету перед сканированием PR');
    focusPreferredScanTarget();
  });
  const openBoxButton = document.getElementById('open-box');
  const closeBoxButton = document.getElementById('close-box');
  const closePalletButton = document.getElementById('close-pallet');
  const scanButton = document.getElementById('scan-submit');
  const saveLocationButton = document.getElementById('save-location');
  const clearLocationButton = document.getElementById('clear-location');
  const finishButton = document.getElementById('finish-button');
  const agentSelect = document.getElementById('print-agent');
  const printerSelect = document.getElementById('print-printer');
  const printStatus = document.getElementById('print-status');
  const refreshPrintAgentsButton = document.getElementById('refresh-print-agents');
  const reprintContainerSelect = document.getElementById('reprint-container');
  const reprintContainerButton = document.getElementById('reprint-container-label');
  const markingModeToggle = document.getElementById('marking-mode-toggle');

  const normalizedScan = (value) => String(value || '').trim().toUpperCase();
  const isOsLocationCode = (value) => {
    const code = normalizedScan(value);
    return /^OS-\d+-\d+-\d+-\d+$/.test(code) || /^[A-ZА-ЯЁ]-\d+\/\d+-\d+$/.test(code);
  };
  const isPrLocationCode = (value) => {
    const code = normalizedScan(value);
    return /^PR(?:[-/][A-ZА-ЯЁ0-9]+)+$/.test(code) || /^BS-\d+\/\d+-\d+$/.test(code);
  };

  const focusPreferredScanTarget = () => {
    if (busy || editingBox || czRecountActive) return;
    const target = preferredScanTarget === 'location' ? locationInput : input;
    window.setTimeout(() => {
      if (!busy && !editingBox && target && !target.disabled) target.focus();
    }, 40);
  };

  const eventId = () => {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return `tsd-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  };

  const setStatus = (text, kind = '') => {
    status.textContent = text;
    status.classList.toggle('is-error', kind === 'error');
    status.classList.toggle('is-ok', kind === 'ok');
    if (editingBox && correctionStatus) correctionStatus.textContent = text;
  };

  const request = async (url, payload) => {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': app.dataset.csrf || '' },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || `Ошибка ${response.status}`);
      error.data = data;
      throw error;
    }
    return data;
  };

  const activeBox = () => {
    const code = String(state.activeBox || '');
    return (state.boxes || []).find((box) => String(box.code || '') === code && !box.sealed) || null;
  };

  const activePallet = () => {
    const code = String(state.activePallet || '');
    return (state.pallets || []).find((pallet) => String(pallet.code || '') === code && !pallet.sealed) || null;
  };

  const renderPutaway = () => {
    const host = document.getElementById('putaway-preview');
    host.innerHTML = '';
    if (!putawayPreview.length) {
      host.innerHTML = '<p class="muted">Закройте палету — она останется здесь до завершения размещения.</p>';
      return;
    }
    putawayPreview.forEach((row) => {
      const item = document.createElement('div');
      item.className = 'putaway-row';
      if (row.needs_pr_location) item.classList.add('needs-location');
      const head = document.createElement('div');
      head.className = 'putaway-row-head';
      const pallet = document.createElement('strong');
      pallet.textContent = row.pallet_code || 'Палета';
      const statusLabel = document.createElement('span');
      statusLabel.className = 'putaway-status';
      statusLabel.textContent = row.placement_status_label || 'Ожидает размещения';
      head.append(pallet, statusLabel);

      const current = document.createElement('div');
      current.className = 'putaway-route-line';
      const currentCaption = document.createElement('span');
      currentCaption.textContent = 'Сейчас';
      const currentValue = document.createElement('b');
      currentValue.textContent = row.current_location_label || 'PR не указано';
      current.append(currentCaption, currentValue);

      const destination = document.createElement('div');
      destination.className = 'putaway-route-line';
      const destinationCaption = document.createElement('span');
      destinationCaption.textContent = 'Далее';
      const destinationValue = document.createElement('b');
      destinationValue.textContent = row.destination_label || row.destination_code || 'Место не назначено';
      destination.append(destinationCaption, destinationValue);

      item.append(head, current, destination);
      if (row.needs_pr_location) {
        const choose = document.createElement('button');
        choose.type = 'button';
        choose.className = 'button button-secondary putaway-location-action';
        choose.textContent = 'Указать место PR';
        choose.disabled = busy || editingBox;
        choose.addEventListener('click', () => {
          placementPalletCode = String(row.pallet_code || '').trim();
          renderLocation();
          locationInput.value = '';
          preferredScanTarget = 'location';
          setStatus(`Отсканируйте место PR для палеты ${placementPalletCode}`);
          focusPreferredScanTarget();
        });
        item.appendChild(choose);
      }
      host.appendChild(item);
    });
  };

  const renderLocation = () => {
    document.getElementById('location-label').textContent = receivingLocation.label || 'Не указано';
    document.getElementById('location-code').textContent = receivingLocation.code || 'PR';
    if (!pendingPlacementPallets.some((pallet) => pallet.code === placementPalletCode)) placementPalletCode = '';
    if (!locationPallet) return;
    locationPallet.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = pendingPlacementPallets.length ? 'Выберите палету' : 'Нет закрытых палет, ожидающих размещения';
    locationPallet.appendChild(placeholder);
    pendingPlacementPallets.forEach((pallet) => {
      const option = document.createElement('option');
      option.value = pallet.code;
      option.textContent = `${pallet.code} · ${pallet.qty} шт.`;
      locationPallet.appendChild(option);
    });
    locationPallet.value = placementPalletCode;
    locationPallet.disabled = busy || editingBox || !pendingPlacementPallets.length;
  };

  const render = (nextSummary) => {
    if (nextSummary) summary = nextSummary;
    const box = activeBox();
    const pallet = activePallet();
    if (editingBox && (!box || box.code !== editingBoxCode)) {
      editingBox = false;
      editingBoxCode = '';
    }
    document.getElementById('accepted-qty').textContent = summary.accepted_qty || 0;
    document.getElementById('planned-qty').textContent = summary.planned_qty || 0;
    document.getElementById('remaining-qty').textContent = summary.remaining_qty || 0;
    document.getElementById('active-box-code').textContent = box?.code || 'Не открыт';
    document.getElementById('active-box-qty').textContent = summary.active_box_qty || 0;
    document.getElementById('active-pallet-code').textContent = pallet?.code || 'Не открыта';
    document.getElementById('active-pallet-qty').textContent = summary.active_pallet_qty || 0;
    document.getElementById('active-pallet-boxes').textContent = summary.active_pallet_boxes || 0;
    document.getElementById('sealed-pallet-count').textContent = summary.sealed_pallet_count || 0;
    document.getElementById('box-count').textContent = `всего коробов: ${summary.box_count || 0}`;
    const percent = summary.planned_qty > 0 ? Math.min((summary.accepted_qty / summary.planned_qty) * 100, 100) : 0;
    document.getElementById('progress-bar').style.width = `${percent}%`;

    czRecountStart.disabled = busy || editingBox || czRecountActive || !isMarked || !box || Number(summary.active_box_qty || 0) <= 0;
    openBoxButton.disabled = busy || editingBox || Boolean(box);
    closeBoxButton.disabled = busy || editingBox || !box;
    closePalletButton.disabled = busy || editingBox || Boolean(box) || !pallet || Number(summary.active_pallet_qty || 0) <= 0;
    scanButton.disabled = busy || editingBox;
    input.disabled = busy || editingBox;
    if (quantityInput) quantityInput.disabled = busy || editingBox || Boolean(pendingBarcode);
    saveLocationButton.disabled = busy || editingBox;
    clearLocationButton.disabled = busy || editingBox;
    locationInput.disabled = busy || editingBox;
    if (editBoxButton) {
      editBoxButton.disabled = busy || !box || !pallet || !app.dataset.correctBoxUrl;
      editBoxButton.textContent = editingBox ? 'Вернуться к сканированию' : 'Исправить короб';
      correctionPanel.hidden = !editingBox;
      correctionMark.disabled = busy;
      removeCorrectionMark.disabled = busy;
      finishCorrection.disabled = busy;
    }
    finishButton.disabled = (
      busy || editingBox
      || Boolean(box)
      || Boolean(pallet)
      || Number(summary.sealed_pallet_count || 0) <= 0
      || !String(receivingLocation.code || '').trim()
      || pendingPlacementPallets.length > 0
    );
    if (markingModeToggle) {
      markingModeToggle.disabled = busy || editingBox || app.dataset.markingModeCanChange !== '1';
    }

    const items = document.getElementById('current-items');
    items.innerHTML = '';
    if (!box || !(box.items || []).length) {
      items.innerHTML = '<p class="muted">Товаров в текущем коробе пока нет.</p>';
    } else {
      (box.items || []).forEach((item, rowIndex) => {
        const row = document.createElement('div');
        row.className = 'item-row';
        const copy = document.createElement('div');
        const name = document.createElement('strong');
        name.textContent = item.name || item.sku_code || 'Товар';
        const meta = document.createElement('small');
        meta.textContent = `${item.sku_code || ''} · ${item.barcode || ''}`;
        const qty = document.createElement('b');
        qty.textContent = Number(item.qty || 0);
        copy.append(name, meta);
        row.append(copy, qty);
        if (editingBox && !isMarked) {
          const controls = document.createElement('div');
          controls.className = 'box-correction-controls';
          const minus = document.createElement('button');
          minus.type = 'button';
          minus.className = 'button button-secondary';
          minus.textContent = '−1';
          minus.setAttribute('aria-label', `Убрать одну штуку: ${item.name || item.sku_code}`);
          minus.disabled = busy;
          const label = document.createElement('label');
          label.textContent = 'Верное кол-во';
          const quantity = document.createElement('input');
          quantity.type = 'number';
          quantity.inputMode = 'numeric';
          quantity.min = '0';
          quantity.max = String(Number(item.qty || 0));
          quantity.step = '1';
          quantity.value = String(Number(item.qty || 0));
          quantity.disabled = busy;
          quantity.addEventListener('focus', () => quantity.select());
          label.appendChild(quantity);
          const save = document.createElement('button');
          save.type = 'button';
          save.className = 'button button-primary';
          save.textContent = 'Сохранить';
          save.disabled = busy;
          const correct = (value) => {
            if (!/^\d+$/.test(String(value)) || Number(value) >= Number(item.qty)) {
              setStatus('Введите целое количество от 0 до текущего минус один.', 'error');
              return;
            }
            if (!window.confirm(`${item.name || item.sku_code}: ${item.qty} → ${value} шт. Сохранить?`)) return;
            correctBox({row_index: rowIndex, item, expected_qty: Number(item.qty), quantity: Number(value)});
          };
          minus.addEventListener('click', () => correct(Number(item.qty) - 1));
          save.addEventListener('click', () => correct(quantity.value));
          quantity.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') { event.preventDefault(); correct(quantity.value); }
          });
          controls.append(minus, label, save);
          row.appendChild(controls);
        }
        items.appendChild(row);
      });
    }
    renderLocation();
    renderPutaway();
    renderReprintContainers();
    focusPreferredScanTarget();
  };

  const applyResponse = (data) => {
    if (data.state) state = data.state;
    if (data.summary) summary = data.summary;
    if (data.receiving_location) receivingLocation = data.receiving_location;
    if (Array.isArray(data.pending_placement_pallets)) pendingPlacementPallets = data.pending_placement_pallets;
    if (Array.isArray(data.putaway_preview)) putawayPreview = data.putaway_preview;
    if (data.metrics) {
      document.getElementById('metric-rate').textContent = data.metrics.units_per_minute || 0;
      document.getElementById('metric-errors').textContent = data.metrics.error_count || 0;
    }
    render();
  };

const czRecountStart = document.getElementById('cz-recount-start');
const czRecountModal = document.getElementById('cz-recount-modal');
const czRecountCard = document.getElementById('cz-recount-card');
const czRecountClose = document.getElementById('cz-recount-close');
const czRecountCaption = document.getElementById('cz-recount-caption');
const czRecountScansEl = document.getElementById('cz-recount-scans');
const czRecountUniqueEl = document.getElementById('cz-recount-unique');
const czRecountMatchedEl = document.getElementById('cz-recount-matched');
const czRecountDuplicatesEl = document.getElementById('cz-recount-duplicates');
const czRecountUnexpectedEl = document.getElementById('cz-recount-unexpected');
const czRecountMissingEl = document.getElementById('cz-recount-missing');
const czRecountStatus = document.getElementById('cz-recount-status');
const czRecountAlert = document.getElementById('cz-recount-alert');
const czRecountIssues = document.getElementById('cz-recount-issues');
const czRecountRestart = document.getElementById('cz-recount-restart');
const czRecountAck = document.getElementById('cz-recount-ack');
        let czRecountActive = false;
        let czRecountIssueBlocked = false;
        let czRecountBoxCode = '';
        let czRecountScans = 0;
        let czRecountDuplicateCount = 0;
        let czRecountUnexpectedCount = 0;
        let czRecountExpected = new Map();
        let czRecountSeen = new Map();
        let czRecountIssueRows = [];

      const normalizeCzRecountCode = (value) => {
        let code = String(value || '').trim();
        if (code.slice(0, 3).toLowerCase() === ']d2') {
          code = code.slice(3);
        }
        code = code
          .replace(/_x001d_/gi, '\x1d')
          .replace(/<\s*gs\s*>/gi, '\x1d')
          .replace(/\{\s*fnc1\s*\}/gi, '\x1d')
          .replace(/(?:\\u001d|\\x001d|\\x1d)/gi, '\x1d');
        return code.trim();
      };
      const czRecountIdentity = (value) => {
        const code = normalizeCzRecountCode(value);
        const looksLikeGs1 = (
          code.length >= 19
          && code.startsWith('01')
          && /^\d{14}$/.test(code.slice(2, 16))
          && code.slice(16, 18) === '21'
        );
        if (!looksLikeGs1) {
          return code;
        }
        const prefix = code.slice(0, 18);
        const tail = code.slice(18);
        if (tail.includes('\x1d')) {
          return `${prefix}${tail.split('\x1d', 1)[0]}`;
        }
        for (let index = 0; index < Math.max(0, tail.length - 7); index += 1) {
          if (
            tail.slice(index, index + 2) === '91'
            && ['92', '93'].includes(tail.slice(index + 6, index + 8))
          ) {
            return `${prefix}${tail.slice(0, index)}`;
          }
        }
        return code;
      };
      const czRecountCodeHint = (identity) => {
        const value = String(identity || '');
        if (
          value.startsWith('01')
          && /^\d{14}$/.test(value.slice(2, 16))
          && value.slice(16, 18) === '21'
        ) {
          const serial = value.slice(18);
          const shortSerial = serial.length > 24
            ? `${serial.slice(0, 14)}…${serial.slice(-6)}`
            : serial;
          return `(21) ${shortSerial || '-'}`;
        }
        return value.length > 30 ? `${value.slice(0, 18)}…${value.slice(-8)}` : (value || '-');
      };
      const collectCzRecountExpected = (box) => {
        const expected = new Map();
        (czRecountLoadedCodes || []).forEach((code) => {
          const identity = czRecountIdentity(code);
          if (identity) expected.set(identity, {hint:czRecountCodeHint(identity)});
        });
        return expected;
      };
      const getCzRecountStats = () => {
        let matched = 0;
        czRecountSeen.forEach((entry, identity) => {
          if (czRecountExpected.has(identity)) {
            matched += 1;
          }
        });
        return {
          scans: czRecountScans,
          unique: czRecountSeen.size,
          matched,
          duplicates: czRecountDuplicateCount,
          unexpected: czRecountUnexpectedCount,
          missing: Math.max(0, czRecountExpected.size - matched),
        };
      };
      const renderCzRecount = () => {
        const stats = getCzRecountStats();
        if (czRecountScansEl) czRecountScansEl.textContent = String(stats.scans);
        if (czRecountUniqueEl) czRecountUniqueEl.textContent = String(stats.unique);
        if (czRecountMatchedEl) czRecountMatchedEl.textContent = String(stats.matched);
        if (czRecountDuplicatesEl) czRecountDuplicatesEl.textContent = String(stats.duplicates);
        if (czRecountUnexpectedEl) czRecountUnexpectedEl.textContent = String(stats.unexpected);
        if (czRecountMissingEl) czRecountMissingEl.textContent = String(stats.missing);
        if (czRecountIssues) {
          czRecountIssues.innerHTML = '';
          czRecountIssues.hidden = czRecountIssueRows.length === 0;
          czRecountIssueRows.forEach((issue) => {
            const row = document.createElement('div');
            row.className = 'cz-recount-issue-row';
            row.textContent = issue;
            czRecountIssues.appendChild(row);
          });
        }
      };
      const clearCzRecountIssue = () => {
        czRecountIssueBlocked = false;
        czRecountCard?.classList.remove('is-issue');
        if (czRecountAlert) {
          czRecountAlert.hidden = true;
          czRecountAlert.textContent = '';
        }
        if (czRecountAck) {
          czRecountAck.hidden = true;
        }
        if (czRecountClose) {
          czRecountClose.disabled = false;
        }
        if (czRecountRestart) {
          czRecountRestart.disabled = false;
        }
      };
      const showCzRecountIssue = (message) => {
        czRecountIssueBlocked = true;
        czRecountCard?.classList.add('is-issue');
        if (czRecountAlert) {
          czRecountAlert.textContent = message;
          czRecountAlert.hidden = false;
        }
        if (czRecountStatus) {
          czRecountStatus.textContent = 'Сканирование остановлено. Отложите товар в руке и подтвердите предупреждение.';
        }
        if (czRecountAck) {
          czRecountAck.hidden = false;
        }
        if (czRecountClose) {
          czRecountClose.disabled = true;
        }
        if (czRecountRestart) {
          czRecountRestart.disabled = true;
        }
        setScannerStatus(message, true);
        playScanErrorSound();
        window.setTimeout(playScanErrorSound, 420);
      };
      const startCzRecount = (restart = false) => {
        if (busy || editingBox || czRecountLoading) {
          setScannerStatus('Дождитесь завершения текущей операции перед перепроверкой.', true);
          return;
        }
        const box = restart ? getBox(czRecountBoxCode) : activeBox();
        const expected = collectCzRecountExpected(box);
        if (!box || !expected.size) {
          setScannerStatus('В текущем коробе нет принятых ЧЗ для перепроверки.', true);
          playScanErrorSound();
          return;
        }
        pendingBarcode = ''; 
        czRecountActive = true;
        czRecountBoxCode = box.code;
        czRecountScans = 0;
        czRecountDuplicateCount = 0;
        czRecountUnexpectedCount = 0;
        czRecountExpected = expected;
        czRecountSeen = new Map();
        czRecountIssueRows = [];
        clearCzRecountIssue();
        if (czRecountCaption) {
          czRecountCaption.textContent = (
            `Короб ${box.code}. В системе ${expected.size} уникальных ЧЗ. `
            + 'Сканируйте только DataMatrix каждого физического товара по одному разу. Данные короба не изменяются. '
            + 'Не обновляйте страницу до окончания: прогресс этой проверки хранится только в открытом окне.'
          );
        }
        if (czRecountStatus) {
          czRecountStatus.textContent = 'Перепроверка начата. Сканируйте первый DataMatrix ЧЗ.';
        }
        if (!czRecountModal.open) czRecountModal.showModal();
        czRecountModal?.setAttribute('aria-hidden', 'false');
        if (czRecountStart) {
          czRecountStart.disabled = true;
        }
        renderCzRecount();
        setScannerStatus(`Перепроверка короба ${box.code}: ожидаю DataMatrix ЧЗ`, false);
        window.setTimeout(() => barcodeInput?.focus(), 0);
      };
      const acknowledgeCzRecountIssue = () => {
        clearCzRecountIssue();
        if (czRecountStatus) {
          czRecountStatus.textContent = 'Предупреждение подтверждено. Продолжайте сканирование следующего товара.';
        }
        setScannerStatus(`Перепроверка короба ${czRecountBoxCode}: продолжайте сканирование`, false);
        window.setTimeout(() => barcodeInput?.focus(), 0);
      };
      const finishCzRecount = () => {
        if (!czRecountActive) {
          return;
        }
        if (czRecountIssueBlocked) {
          setScannerStatus('Сначала подтвердите найденную проблему ЧЗ.', true);
          return;
        }
        const stats = getCzRecountStats();
        const boxCode = czRecountBoxCode;
        czRecountActive = false;
        czRecountBoxCode = '';
        czRecountModal.close();
        czRecountModal?.setAttribute('aria-hidden', 'true');
        render();
        const summary = (
          `Перепроверка ${boxCode}: сканов ${stats.scans}, уникальных ${stats.unique}, `
          + `дублей ${stats.duplicates}, новых ${stats.unexpected}, осталось проверить ${stats.missing}.`
        );
        setScannerStatus(summary, stats.duplicates > 0 || stats.unexpected > 0 || stats.missing > 0);
        window.setTimeout(() => barcodeInput?.focus(), 0);
      };
      const handleCzRecountScan = (rawValue) => {
        if (!czRecountActive) {
          return;
        }
        if (czRecountIssueBlocked) {
          setScannerStatus('Сканирование остановлено: подтвердите найденную проблему ЧЗ.', true);
          return;
        }
        const values = String(rawValue || '').split(/[\r\n]+/).map((value) => value.trim()).filter(Boolean);
        if (values.length !== 1) {
          setScannerStatus('Перепроверка: сканируйте один DataMatrix ЧЗ за раз.', true);
          playScanErrorSound();
          return;
        }
        const code = normalizeCzRecountCode(values[0]);
        if (!isDataMatrixValue(code)) {
          setScannerStatus('Перепроверка принимает только DataMatrix ЧЗ, без штрихкода товара.', true);
          playScanErrorSound();
          return;
        }
        const identity = czRecountIdentity(code);
        const hint = czRecountCodeHint(identity);
        if (!identity) {
          setScannerStatus('Не удалось распознать ЧЗ. Повторите сканирование DataMatrix.', true);
          playScanErrorSound();
          return;
        }
        czRecountScans += 1;
        const previous = czRecountSeen.get(identity);
        if (previous) {
          previous.count += 1;
          czRecountDuplicateCount += 1;
          const message = (
            `ДУБЛЬ ПРИ ПЕРЕПРОВЕРКЕ: ${hint}. `
            + `Первый скан №${previous.firstScan}, повтор №${czRecountScans}. `
            + 'Отложите товар, который сейчас находится в руке.'
          );
          czRecountIssueRows.push(message);
          renderCzRecount();
          showCzRecountIssue(message);
          return;
        }
        czRecountSeen.set(identity, { firstScan: czRecountScans, count: 1, hint });
        if (!czRecountExpected.has(identity)) {
          czRecountUnexpectedCount += 1;
          const message = (
            `НОВЫЙ ЧЗ НЕ УЧТЁН В КОРОБЕ: ${hint}, скан №${czRecountScans}. `
            + 'Отложите товар, который сейчас находится в руке.'
          );
          czRecountIssueRows.push(message);
          renderCzRecount();
          showCzRecountIssue(message);
          return;
        }
        if (czRecountStatus) {
          czRecountStatus.textContent = `Проверен ${hint}. Скан №${czRecountScans}. Продолжайте.`;
        }
        renderCzRecount();
        setScannerStatus(`Перепроверка: принят ${hint}, скан №${czRecountScans}`, false);
        playCzScanSuccessSound();
      };


  let czRecountLoading = false;
  let czRecountLoadedCodes = [];
  const barcodeInput = document.getElementById('cz-recount-input');
  const getBox = code => (state.boxes || []).find(box => box.code === code);
  const setScannerStatus = (text, error) => setStatus(text, error ? 'error' : 'ok');
  const isDataMatrixValue = value => /^01\d{14}21.+/.test(value);
  const playCzScanSuccessSound = () => {};
  const playScanErrorSound = () => { if (navigator.vibrate) navigator.vibrate([100,50,100]); };
  const prepareCzRecount = async () => {
    const box = activeBox();
    if (busy || editingBox || czRecountActive || czRecountLoading || !box) return;
    czRecountLoading = true;
    busy = true;
    render();
    setStatus('Загружаю ЧЗ текущего короба…');
    try {
      const url = new URL(window.location.href);
      url.search = '';
      url.searchParams.set('cz_recount_box',box.code);
      const response = await fetch(url, {credentials:'same-origin',cache:'no-store'});
      const data = await response.json();
      if (!response.ok || !data.ok || data.box_code !== box.code || !Array.isArray(data.marking_codes)) throw Error(data.error || 'Не удалось загрузить ЧЗ короба');
      czRecountLoadedCodes = data.marking_codes;
      busy = false;
      czRecountLoading = false;
      startCzRecount();
    } catch (error) {
      setStatus(error.message || 'Ошибка загрузки ЧЗ', 'error');
    } finally {
      busy = false;
      czRecountLoading = false;
      render();
      if (czRecountActive) barcodeInput.focus();
    }
  };
  czRecountStart.addEventListener('click', prepareCzRecount);
  czRecountClose.addEventListener('click', finishCzRecount);
  czRecountAck.addEventListener('click', acknowledgeCzRecountIssue);
  czRecountRestart.addEventListener('click', () => {
    if (!czRecountScans || confirm('Начать проверку заново? Прогресс этой проверки будет сброшен.')) startCzRecount(true);
  });
  const scanRecountInput = () => { const value=barcodeInput.value; barcodeInput.value=''; handleCzRecountScan(value); };
  barcodeInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); event.stopPropagation(); scanRecountInput(); }
  });
  document.getElementById('cz-recount-submit').addEventListener('click', scanRecountInput);
  czRecountModal.addEventListener('cancel', event => event.preventDefault());
  window.addEventListener('beforeunload', event => {
    if (czRecountActive && czRecountScans) { event.preventDefault(); event.returnValue=''; }
  });

  const submitScan = async () => {
    if (czRecountActive) { handleCzRecountScan(input.value); input.value=''; return; }
    const value = String(input.value || '').trim();
    if (!value || busy || editingBox) return;
    const quantity = pendingBarcode ? 1 : Number(quantityInput?.value || 1);
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 100000) {
      setStatus('Количество должно быть целым числом от 1 до 100000.', 'error');
      quantityInput?.focus();
      quantityInput?.select();
      return;
    }
    if (!pendingBarcode && (isOsLocationCode(value) || isPrLocationCode(value))) {
      input.value = '';
      locationInput.value = normalizedScan(value);
      preferredScanTarget = 'location';
      if (isOsLocationCode(value)) {
        setStatus(`Это место зоны OS: ${normalizedScan(value)}. На этом экране нужно отсканировать место PR.`, 'error');
        render();
        return;
      }
      await saveLocation();
      return;
    }
    preferredScanTarget = 'product';
    busy = true;
    input.value = '';
    setStatus(pendingBarcode ? 'Проверяю Data Matrix…' : 'Проверяю товар…');
    render();
    try {
      const data = await request(app.dataset.scanUrl, {
        barcode: pendingBarcode || value,
        marking_code: pendingBarcode ? value : '',
        quantity,
        event_id: eventId(),
      });
      if (data.requires_marking) {
        pendingBarcode = value;
        setStatus(`Товар ${data.item?.name || data.item?.sku_code || value} найден. Сканируйте Data Matrix`, 'ok');
      } else {
        pendingBarcode = '';
        applyResponse(data);
        if (quantityInput) quantityInput.value = '1';
        const acceptedQuantity = Number(data.accepted_quantity || quantity || 1);
        const accepted = `Принято: ${data.item?.name || data.item?.sku_code || 'товар'} · ${acceptedQuantity} шт.`;
        setStatus(
          data.box_opened_automatically ? `Короб ${data.box_code || ''} открыт автоматически. ${accepted}` : accepted,
          'ok',
        );
      }
    } catch (error) {
      setStatus(error.message || 'Скан не принят', 'error');
    } finally {
      busy = false;
      render();
    }
  };

  openBoxButton.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    pendingBarcode = '';
    setStatus('Создаю короб…');
    render();
    try {
      const data = await request(app.dataset.openBoxUrl, { event_id: eventId() });
      applyResponse(data);
      setStatus('Короб открыт. Сканируйте ШК товара', 'ok');
    } catch (error) {
      setStatus(error.message || 'Короб не создан', 'error');
    } finally {
      busy = false;
      render();
    }
  });

  const updatePrintStatus = () => {
    const selectedAgent = agents.find((item) => String(item.agent_id) === String(agentSelect.value));
    const printerName = String(selectedAgent?.active_printer || '').trim();
    if (!selectedAgent) {
      printStatus.textContent = agents.length
        ? 'Выберите компьютер рядом с местом приемки.'
        : 'Нет подключенных компьютеров. Запустите Fullbox Desktop на свободном компьютере и нажмите «Обновить».';
      return;
    }
    if (!printerName) {
      printStatus.textContent = `На компьютере ${selectedAgent.title} нет готового принтера. Проверьте принтер в верхней строке Fullbox Desktop.`;
      return;
    }
    printStatus.textContent = `Автоматически: ${selectedAgent.title} · ${printerName}. Выбирать принтер на TSD не нужно.`;
  };

  const closedContainers = () => {
    const rows = [];
    (state.boxes || []).forEach((box) => {
      const code = String(box?.code || '').trim();
      if (code && box?.sealed) rows.push({ key: `box:${code}`, code, kind: 'Короб' });
    });
    (state.pallets || []).forEach((pallet) => {
      const code = String(pallet?.code || '').trim();
      if (code && pallet?.sealed) rows.push({ key: `pallet:${code}`, code, kind: 'Палета' });
    });
    return rows.reverse();
  };

  const renderReprintContainers = () => {
    if (!reprintContainerSelect || !reprintContainerButton) return;
    const currentKey = String(reprintContainerSelect.value || '');
    const rows = closedContainers();
    reprintContainerSelect.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = rows.length ? 'Выберите закрытую тару' : 'Нет закрытых коробов или палет';
    reprintContainerSelect.appendChild(placeholder);
    rows.forEach((row) => {
      const option = document.createElement('option');
      option.value = row.key;
      option.textContent = `${row.kind} · ${row.code}`;
      reprintContainerSelect.appendChild(option);
    });
    reprintContainerSelect.value = rows.some((row) => row.key === currentKey) ? currentKey : '';
    reprintContainerSelect.disabled = busy || !rows.length;
    reprintContainerButton.disabled = busy || !rows.length || !reprintContainerSelect.value;
  };

  const updatePrinterOptions = () => {
    const selectedAgent = agents.find((item) => String(item.agent_id) === String(agentSelect.value));
    const activePrinter = String(selectedAgent?.active_printer || '').trim();
    printerSelect.innerHTML = '<option value="">Нет готового принтера</option>';
    if (activePrinter) {
      const option = document.createElement('option');
      option.value = activePrinter;
      option.textContent = activePrinter;
      printerSelect.appendChild(option);
    }
    printerSelect.value = activePrinter;
    printerSelect.disabled = true;
    if (activePrinter) {
      localStorage.setItem('processing_label_printer', activePrinter);
      localStorage.setItem('label_printer', activePrinter);
    }
    updatePrintStatus();
  };

  const renderPrintAgents = (nextAgents, { allowFallback = true } = {}) => {
    const currentAgentId = String(agentSelect.value || '').trim();
    const storedAgentId = localStorage.getItem('labels_print_agent_id') || localStorage.getItem('fullbox_agent_id') || '';
    agents = Array.isArray(nextAgents) ? nextAgents : [];
    agentSelect.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = agents.length ? 'Выберите компьютер' : 'Нет компьютеров в сети';
    agentSelect.appendChild(placeholder);
    agents.forEach((agent) => {
      const option = document.createElement('option');
      option.value = String(agent.agent_id || '');
      option.textContent = `${agent.title || agent.agent_id} · онлайн · ${agent.active_printer || 'нет готового принтера'}`;
      agentSelect.appendChild(option);
    });
    const availableIds = agents.map((item) => String(item.agent_id || ''));
    let nextAgentId = availableIds.includes(currentAgentId) ? currentAgentId : '';
    if (!nextAgentId && allowFallback && availableIds.includes(storedAgentId)) nextAgentId = storedAgentId;
    if (!nextAgentId && allowFallback) nextAgentId = availableIds[0] || '';
    agentSelect.value = nextAgentId;
    if (nextAgentId) {
      localStorage.setItem('labels_print_agent_id', nextAgentId);
      localStorage.setItem('fullbox_agent_id', nextAgentId);
    }
    updatePrinterOptions();
  };

  const refreshPrintAgents = async ({ silent = false, allowFallback = true } = {}) => {
    if (!silent) printStatus.textContent = 'Ищу включенные компьютеры…';
    try {
      const response = await fetch(app.dataset.printAgentsUrl, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || `Ошибка ${response.status}`);
      renderPrintAgents(data.agents, { allowFallback });
      return true;
    } catch (error) {
      if (!silent) printStatus.textContent = error.message || 'Не удалось обновить список компьютеров.';
      return false;
    }
  };

  const logPrintEvent = (eventType, data) => request(app.dataset.eventUrl, {
    event_type: eventType,
    event_id: eventId(),
    ...data,
  }).catch(() => {});

  const printContainerLabel = async (containerCode, kind) => {
    const selectedBeforeRefresh = String(agentSelect.value || '').trim();
    printStatus.textContent = 'Проверяю подключение к компьютеру…';
    await refreshPrintAgents({ silent: true, allowFallback: !selectedBeforeRefresh });
    const agentId = String(agentSelect.value || '').trim();
    const selectedAgent = agents.find((item) => String(item.agent_id) === agentId);
    const printerName = String(selectedAgent?.active_printer || '').trim();
    if (!agentId || !printerName) {
      printStatus.textContent = `${kind} ${containerCode} закрыт(а). На выбранном компьютере нет готового принтера.`;
      return;
    }
    printStatus.textContent = 'Формирую этикетку…';
    const qrHost = document.getElementById('label-qr');
    qrHost.innerHTML = '';
    document.getElementById('label-code').textContent = containerCode;
    document.getElementById('label-kind').textContent = kind.toUpperCase();
    try {
      if (!window.QRCode || !window.html2canvas) throw new Error('Модуль этикетки не загрузился');
      new window.QRCode(qrHost, { text: containerCode, width: 240, height: 240, correctLevel: window.QRCode.CorrectLevel.M });
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const canvas = await window.html2canvas(document.getElementById('receiving-label'), {
        backgroundColor: '#ffffff',
        scale: 2.1,
        logging: false,
        useCORS: true,
      });
      const base64 = canvas.toDataURL('image/png').split(',')[1] || '';
      const data = await request(app.dataset.printUrl, {
        order_id: app.dataset.orderId,
        card_id: '',
        article: kind,
        barcode: containerCode,
        size: '',
        printer_name: printerName,
        agent_id: agentId,
        label_png_base64: base64,
        label_width_mm: 60,
        label_height_mm: 58,
      });
      printStatus.textContent = `Этикетка ${containerCode} отправлена на печать.`;
      logPrintEvent('print_queued', {
        container_type: kind,
        container_code: containerCode,
        job_id: data.job_id,
        agent_id: agentId,
        printer_name: printerName,
      });
    } catch (error) {
      printStatus.textContent = `${kind} закрыт(а), но этикетка не напечатана: ${error.message || 'ошибка'}`;
      logPrintEvent('print_failed', {
        container_type: kind,
        container_code: containerCode,
        agent_id: agentId,
        printer_name: printerName,
        error: error.message || '',
      });
    }
  };

  closeBoxButton.addEventListener('click', async () => {
    if (busy || !window.confirm('Закрыть текущий короб?')) return;
    busy = true;
    pendingBarcode = '';
    setStatus('Закрываю короб…');
    render();
    try {
      const data = await request(app.dataset.closeBoxUrl, { event_id: eventId() });
      applyResponse(data);
      setStatus('Короб закрыт. Откройте следующий короб или закройте палету', 'ok');
      if (data.box_code) await printContainerLabel(data.box_code, 'Короб');
    } catch (error) {
      setStatus(error.message || 'Короб не закрыт', 'error');
    } finally {
      busy = false;
      render();
    }
  });

  closePalletButton.addEventListener('click', async () => {
    if (busy || !window.confirm('Закрыть палету и напечатать ее этикетку?')) return;
    busy = true;
    setStatus('Закрываю палету…');
    render();
    try {
      const data = await request(app.dataset.closePalletUrl, { event_id: eventId() });
      placementPalletCode = data.pallet_code || '';
      preferredScanTarget = 'location';
      applyResponse(data);
      setStatus(`Палета ${placementPalletCode} закрыта. Отсканируйте место PR для разрешения размещения.`, 'ok');
      if (data.pallet_code && data.print_required !== false) {
        await printContainerLabel(data.pallet_code, 'Палета');
      } else if (data.pallet_code) {
        printStatus.textContent = `Этикетка ${data.pallet_code} уже отправлена или напечатана. Повторная печать не требуется.`;
      }
    } catch (error) {
      setStatus(error.message || 'Палета не закрыта', 'error');
    } finally {
      busy = false;
      render();
    }
  });

  const saveLocation = async () => {
    const value = String(locationInput.value || '').trim();
    preferredScanTarget = 'location';
    if (pendingPlacementPallets.length && !placementPalletCode) {
      setStatus('Выберите палету, для которой сканируете место PR. Адрес не сохранён.', 'error');
      return;
    }
    if (!value || busy) {
      if (!value) setStatus('Отсканируйте QR конкретного места PR', 'error');
      return;
    }
    busy = true;
    setStatus('Проверяю место PR…');
    render();
    try {
      let fbsCellId = null;
      if (app.dataset.receivingRoute === 'fbs' && placementPalletCode) {
        const pallet = (state.pallets || []).find(row => row.code === placementPalletCode);
        fbsCellId = await window.chooseReceivingFbsDestination({
          url: app.dataset.fbsDestinationUrl, palletCode: placementPalletCode,
          requiredBoxes: Math.max(1, (pallet?.boxes || []).length),
        });
        if (!fbsCellId) {
          setStatus('Выбор FBS-места отменён. Размещение не создано.');
          return;
        }
      }
      const data = await request(app.dataset.locationUrl, { location_code: value, pallet_code: placementPalletCode, fbs_cell_id: fbsCellId, event_id: eventId() });
      locationInput.value = '';
      applyResponse(data);
      preferredScanTarget = 'product';
      const savedLocation = data.receiving_location?.code || value;
      if (data.placement_pallet_code) {
        const fbsRoute = data.fbs_route && typeof data.fbs_route === 'object' ? data.fbs_route : {};
        const fbsTargets = Array.isArray(fbsRoute.targets) ? fbsRoute.targets : [];
        if (fbsRoute.status === 'blocked') {
          setStatus(fbsRoute.message || `Палета ${data.placement_pallet_code} остаётся в PR: нет свободного FBS-места.`, 'error');
        } else if (fbsTargets.length) {
          const exactDestination = fbsTargets
            .map((target) => `${target.cell_code || target.cell_label} · ${target.pallet_code}`)
            .join(', ');
          setStatus(`Палета ${data.placement_pallet_code}: точное FBS-место ${exactDestination}.`, 'ok');
        } else {
          setStatus(`Место сохранено: ${savedLocation}. Палета ${data.placement_pallet_code} готова к забору водителем.`, 'ok');
        }
      } else {
        setStatus(`Место приемки сохранено: ${savedLocation}`, 'ok');
      }
    } catch (error) {
      setStatus(error.message || 'Место не сохранено', 'error');
    } finally {
      busy = false;
      render();
    }
  };

  const clearLocation = () => {
    if (busy) return;
    locationInput.value = '';
    pendingBarcode = '';
    preferredScanTarget = 'location';
    setStatus('Поле очищено. Отсканируйте QR конкретного места PR.', 'ok');
    render();
  };

  if (markingModeToggle) {
    markingModeToggle.addEventListener('change', async () => {
      if (busy || app.dataset.markingModeCanChange !== '1') return;
      const enabled = markingModeToggle.checked;
      busy = true;
      pendingBarcode = '';
      setStatus(enabled ? 'Включаю сканирование Честного знака…' : 'Переключаю на обычную приемку…');
      render();
      try {
        const data = await request(app.dataset.markingModeUrl, { enabled });
        setStatus(data.message || 'Режим приемки обновлен.', 'ok');
        window.location.reload();
      } catch (error) {
        markingModeToggle.checked = !enabled;
        setStatus(error.message || 'Режим Честного знака не изменен', 'error');
        busy = false;
        render();
      }
    });
  }

  const correctBox = async (change) => {
    if (busy || !editingBox || !editingBoxCode) return;
    const boxCode = editingBoxCode;
    busy = true;
    pendingBarcode = '';
    setStatus('Сохраняю исправление…');
    render();
    try {
      const data = await request(app.dataset.correctBoxUrl, {
        box_code: boxCode, event_id: eventId(), ...change,
      });
      if (correctionMark) correctionMark.value = '';
      applyResponse(data);
      setStatus(data.message || 'Короб исправлен', 'ok');
    } catch (error) {
      if (error.data?.state) applyResponse(error.data);
      setStatus(error.message || 'Исправление не сохранено', 'error');
    } finally {
      busy = false;
      render();
      if (editingBox && isMarked && correctionMark) correctionMark.focus();
    }
  };
  const finishEditing = () => {
    if (busy) return;
    editingBox = false;
    editingBoxCode = '';
    if (correctionMark) correctionMark.value = '';
    preferredScanTarget = 'product';
    setStatus('Сканируйте ШК товара');
    render();
  };
  if (editBoxButton) editBoxButton.addEventListener('click', () => {
    if (busy) return;
    if (editingBox) return finishEditing();
    const box = activeBox();
    if (!box || !activePallet()) return;
    editingBox = true;
    editingBoxCode = box.code;
    pendingBarcode = '';
    input.value = '';
    render();
    const card = document.getElementById('current-items-card');
    if (card) {
      card.open = true;
      card.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    setStatus(isMarked ? 'Отсканируйте лишний Data Matrix' : 'Укажите верное количество товара');
    if (isMarked && correctionMark) correctionMark.focus();
  });
  if (finishCorrection) finishCorrection.addEventListener('click', finishEditing);
  const removeMark = () => {
    if (busy || !editingBox || !isMarked) return;
    const value = String(correctionMark.value || '').trim();
    if (!value) return setStatus('Отсканируйте лишний Data Matrix', 'error');
    if (!window.confirm('Убрать из текущего короба одну единицу с этим Data Matrix?')) return;
    correctBox({ marking_code: value });
  };
  if (removeCorrectionMark) removeCorrectionMark.addEventListener('click', removeMark);
  if (correctionMark) correctionMark.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      removeMark();
    }
  });

  scanButton.addEventListener('click', submitScan);
  input.addEventListener('focus', () => { preferredScanTarget = 'product'; });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      submitScan();
    }
  });
  saveLocationButton.addEventListener('click', saveLocation);
  clearLocationButton.addEventListener('click', clearLocation);
  locationInput.addEventListener('focus', () => { preferredScanTarget = 'location'; });
  locationInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      saveLocation();
    }
  });
  agentSelect.addEventListener('change', () => {
    localStorage.setItem('labels_print_agent_id', agentSelect.value || '');
    localStorage.setItem('fullbox_agent_id', agentSelect.value || '');
    updatePrinterOptions();
  });
  if (reprintContainerSelect) {
    reprintContainerSelect.addEventListener('change', renderReprintContainers);
  }
  if (reprintContainerButton) {
    reprintContainerButton.addEventListener('click', async () => {
      if (busy) return;
      const selected = closedContainers().find((row) => row.key === reprintContainerSelect.value);
      if (!selected) {
        printStatus.textContent = 'Выберите закрытый короб или палету для повторной печати.';
        renderReprintContainers();
        return;
      }
      const agentId = String(agentSelect.value || '').trim();
      const selectedAgent = agents.find((item) => String(item.agent_id) === agentId);
      const printerName = String(selectedAgent?.active_printer || '').trim();
      if (!agentId || !printerName) {
        printStatus.textContent = 'На выбранном компьютере нет готового принтера. Проверьте Fullbox Desktop.';
        return;
      }
      if (!window.confirm(`Повторно напечатать ${selected.kind.toLowerCase()} ${selected.code} на принтере ${printerName}?`)) return;
      busy = true;
      render();
      try {
        await printContainerLabel(selected.code, selected.kind);
      } finally {
        busy = false;
        render();
      }
    });
  }
  refreshPrintAgentsButton.addEventListener('click', () => refreshPrintAgents({ silent: false, allowFallback: true }));
  finishButton.addEventListener('click', async () => {
    if (busy) return;
    if (activeBox()) {
      setStatus('Сначала закройте текущий короб', 'error');
      return;
    }
    if (activePallet()) {
      setStatus('Сначала закройте текущую палету', 'error');
      return;
    }
    if (!receivingLocation.code) {
      setStatus('Сначала отсканируйте место приемки PR', 'error');
      locationInput.focus();
      return;
    }
    if (!window.confirm(`Завершить приемку? Принято ${summary.accepted_qty || 0} из ${summary.planned_qty || 0}. Задания будут переданы ричтраку.`)) return;
    busy = true;
    setStatus('Завершаю приемку и создаю задания ричтраку…');
    render();
    try {
      const data = await request(app.dataset.completeUrl, { event_id: eventId() });
      setStatus(data.message || 'Приемка завершена', 'ok');
      window.location.assign(data.redirect_url || '/orders/receiving/tsd/');
    } catch (error) {
      setStatus(error.message || 'Приемка не завершена', 'error');
      busy = false;
      render();
    }
  });

  const storedAgent = localStorage.getItem('labels_print_agent_id') || localStorage.getItem('fullbox_agent_id') || '';
  if (storedAgent && Array.from(agentSelect.options).some((option) => option.value === storedAgent)) {
    agentSelect.value = storedAgent;
  } else {
    const online = agents.find((item) => item.online && item.agent_id);
    if (online) agentSelect.value = online.agent_id;
  }
  updatePrinterOptions();
  refreshPrintAgents({ silent: true, allowFallback: true });
  window.setInterval(() => {
    const hasSelectedAgent = Boolean(String(agentSelect.value || '').trim());
    refreshPrintAgents({ silent: true, allowFallback: !hasSelectedAgent });
  }, 10000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshPrintAgents({ silent: true, allowFallback: true });
  });
  render(summary);
})();
