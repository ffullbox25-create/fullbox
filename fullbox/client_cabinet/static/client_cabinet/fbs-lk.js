(function () {
  "use strict";

  var root = document.querySelector('.fbs-lk[data-page="/fbs"]');
  if (!root) return;

  var clientId = root.getAttribute("data-client-id") || "";
  var cache = {};
  var pendingReads = Object.create(null);
  var orderPage = 1;
  var orderPages = 1;
  var sourceFileName = "";
  var movementStockByBarcode = {};
  var movementMixedBoxByCode = {};
  var selectedMovementMixedBoxCodes = [];
  var movementStockSort = { key: "", dir: "asc", type: "text" };
  var outboundStock = [];
  var outboundSubmitting = false;
  var outboundImporting = false;
  function newOutboundIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (char) {
      var random = Math.floor(Math.random() * 16);
      var value = char === "x" ? random : (random & 3) | 8;
      return value.toString(16);
    });
  }
  var outboundIdempotencyKey = newOutboundIdempotencyKey();
  var movementDraftKey = "fullbox:fbs:movement-draft:" + (clientId || "current");
  var MOVEMENT_DRAFT_VERSION = 8;
  function newMovementIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "fbs-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2) + "-" + Math.random().toString(36).slice(2);
  }
  var movementIdempotencyKey = newMovementIdempotencyKey();
  var movementStockLoaded = false;
  var movementStockLoading = false;
  var movementSubmitting = false;
  var MOVEMENT_QTY_COMMIT_DELAY_MS = 3500;
  var pendingMovementQtyCommits = Object.create(null);
  var ozonWarehouseSettingsLoaded = false;
  var ozonWarehouseSettingsSaving = false;

  function byId(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function number(value) { return Number(value || 0).toLocaleString("ru-RU"); }
  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }
  function hashInfo() {
    var raw = String(location.hash || "#/fbs").replace(/^#/, "");
    var parts = raw.split("?");
    var params = new URLSearchParams(parts[1] || "");
    return { path: parts[0] || "/", params: params };
  }
  function clientUrl(path) {
    var separator = path.indexOf("?") >= 0 ? "&" : "?";
    return path + (clientId ? separator + "client=" + encodeURIComponent(clientId) : "");
  }
  function api(path, options) {
    var url = clientUrl(path);
    var readOnly = !options || !options.method || String(options.method).toUpperCase() === "GET";
    if (readOnly && pendingReads[url]) return pendingReads[url];
    var result = fetch(url, Object.assign({ credentials: "same-origin" }, options || {}))
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok || !payload.ok) {
            var error = new Error(payload.error || "Не удалось получить данные FBS.");
            error.details = payload.details || [];
            throw error;
          }
          return payload.data;
        });
      });
    if (readOnly) {
      // Share only in-flight GETs. Do not cache stock responses or combine writes.
      pendingReads[url] = result.finally(function () { delete pendingReads[url]; });
      return pendingReads[url];
    }
    return result;
  }
  function showAlert(message) {
    var alert = byId("fbs-alert");
    if (!alert) return;
    alert.textContent = message || "";
    alert.hidden = !message;
  }
  function fail(error) {
    showAlert(error && error.message ? error.message : "Не удалось загрузить раздел FBS.");
  }
  function pill(label, tone) {
    return '<span class="fbs-pill ' + esc(tone || "") + '">' + esc(label || "-") + "</span>";
  }
  function emptyRow(columns, text) {
    return '<tr><td colspan="' + columns + '" class="fbs-empty">' + esc(text) + "</td></tr>";
  }
  function kpi(label, value, meta, tone, href) {
    var tag = href ? "a" : "div";
    var attrs = href ? ' href="' + esc(href) + '"' : "";
    return "<" + tag + ' class="fbs-kpi ' + esc(tone || "") + '"' + attrs + ">" +
      "<span>" + esc(label) + "</span><strong>" + esc(value) + "</strong>" +
      (meta ? "<small>" + esc(meta) + "</small>" : "") +
      "</" + tag + ">";
  }
  function detailTable(headers, rows) {
    var head = headers.map(function (item) { return "<th>" + esc(item) + "</th>"; }).join("");
    return '<div class="fbs-table-wrap"><table class="fbs-table"><thead><tr>' + head +
      "</tr></thead><tbody>" + rows.join("") + "</tbody></table></div>";
  }

  function requestedTab(info) {
    if (info.params.get("order")) return "order-detail";
    if (info.params.get("barcode")) return "stock-detail";
    if (info.params.get("movement")) return "movement-detail";
    return info.params.get("tab") || "overview";
  }
  function parentTab(tab) {
    if (tab === "order-detail") return "orders";
    if (tab === "stock-detail") return "stock";
    if (tab === "movement-detail" || tab === "movement-new") return "movements";
    if (tab === "outbound-new") return "outbound";
    return tab;
  }
  function activatePanel(tab) {
    var valid = ["overview", "orders", "order-detail", "stock", "stock-detail", "reports", "movements", "movement-detail", "movement-new", "outbound", "outbound-new", "totes"];
    if (valid.indexOf(tab) < 0) tab = "overview";
    root.querySelectorAll("[data-fbs-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-fbs-panel") !== tab;
    });
    root.querySelectorAll("[data-fbs-tab]").forEach(function (link) {
      link.classList.toggle("active", link.getAttribute("data-fbs-tab") === parentTab(tab));
    });
    return tab;
  }

  function renderOverview(data) {
    cache.overview = data;
    var head = byId("fbs-head-status");
    if (head) head.textContent = data.enabled ? "FBS подключен" : "FBS выключен";
    byId("fbs-overview-kpis").innerHTML = [
      kpi("Заказов всего", number(data.orders.total), "За весь период", "", "#/fbs?tab=orders"),
      kpi("Заказ поступил", number(data.orders.received), "заказов", "", "#/fbs?tab=orders"),
      kpi("Товар подобран", number(data.orders.picked), "заказов", "accent", "#/fbs?tab=orders"),
      kpi("Заказ отгружен", number(data.orders.shipped), "заказов", "", "#/fbs?tab=orders")
    ].join("");

    byId("fbs-profile-count").textContent = number(data.profiles.length) + " проф.";
    byId("fbs-profiles").innerHTML = data.profiles.length ? data.profiles.map(function (profile) {
      var online = profile.is_active && profile.order_pull_enabled;
      return '<div class="fbs-profile-row"><div><strong><i class="fbs-dot ' + (online ? "ok" : "warn") + '"></i>' +
        esc(profile.marketplace_label) + " · " + esc(profile.name) +
        "</strong><small>Склад " + esc(profile.warehouse || "-") + "</small></div><span>" +
        (online ? "Заказы включены" : profile.is_active ? "Получение выключено" : "Профиль выключен") +
        "</span></div>";
    }).join("") : '<p class="fbs-empty">Профили FBS не настроены.</p>';

    var attention = [];
    if (data.stock.needs_replenishment) attention.push({ title: "Остатки ниже минимума", meta: data.stock.needs_replenishment + " SKU", href: "#/fbs?tab=stock" });
    if (data.movement_requests_open) attention.push({ title: "Открытые перемещения", meta: data.movement_requests_open + " заявок", href: "#/fbs?tab=movements" });
    if (data.outbound_requests_open) attention.push({ title: "Заявки на вывоз", meta: data.outbound_requests_open + " заявок", href: "#/fbs?tab=outbound" });
    byId("fbs-attention").innerHTML = attention.length ? attention.map(function (item) {
      return '<a class="fbs-attention-row" href="' + esc(item.href) + '"><div><strong>' + esc(item.title) +
        "</strong><small>" + esc(item.meta) + "</small></div><span>Открыть</span></a>";
    }).join("") : '<p class="fbs-empty">Нет задач, требующих внимания.</p>';
  }

  function loadOverview(force) {
    if (cache.overview && !force) { renderOverview(cache.overview); return Promise.resolve(); }
    return api("/client/api/v1/fbs/").then(renderOverview).catch(fail);
  }

  function renderOzonWarehouseSettings(data) {
    var status = byId("fbs-ozon-warehouses-status");
    var list = byId("fbs-ozon-warehouses-list");
    var save = byId("fbs-ozon-warehouses-save");
    var warehouses = data.warehouses || [];
    status.classList.toggle("error", Boolean(data.error));
    status.textContent = data.error || ("Выбрано складов: " + number(data.selected_count));
    list.innerHTML = warehouses.length ? warehouses.map(function (warehouse) {
      var meta = "ID " + warehouse.warehouse_id + (warehouse.status ? " · " + warehouse.status : "");
      return '<label class="fbs-warehouse-option"><input type="checkbox" value="' + esc(warehouse.warehouse_id) + '"' +
        (warehouse.selected ? " checked" : "") + '><span><strong>' + esc(warehouse.name || warehouse.warehouse_id) +
        "</strong><small>" + esc(meta) + "</small></span></label>";
    }).join("") : '<p class="fbs-empty">Доступные склады Ozon не найдены.</p>';
    save.disabled = Boolean(data.error) || !data.credentials_configured || !data.client_id_configured || !warehouses.length;
    ozonWarehouseSettingsLoaded = true;
  }

  function loadOzonWarehouseSettings(force) {
    var settings = byId("fbs-ozon-warehouses-settings");
    settings.hidden = false;
    if (ozonWarehouseSettingsLoaded && !force) return Promise.resolve();
    byId("fbs-ozon-warehouses-status").textContent = "Загрузка складов...";
    byId("fbs-ozon-warehouses-list").innerHTML = "";
    byId("fbs-ozon-warehouses-save").disabled = true;
    return api("/client/api/v1/fbs/ozon-warehouses/").then(renderOzonWarehouseSettings).catch(function (error) {
      byId("fbs-ozon-warehouses-status").classList.add("error");
      byId("fbs-ozon-warehouses-status").textContent = error.message;
      byId("fbs-ozon-warehouses-list").innerHTML = "";
    });
  }

  function saveOzonWarehouseSettings() {
    if (ozonWarehouseSettingsSaving) return;
    ozonWarehouseSettingsSaving = true;
    var save = byId("fbs-ozon-warehouses-save");
    var warehouseIds = Array.from(
      byId("fbs-ozon-warehouses-list").querySelectorAll('input[type="checkbox"]:checked')
    ).map(function (input) { return input.value; });
    save.disabled = true;
    save.textContent = "Сохраняем...";
    api("/client/api/v1/fbs/ozon-warehouses/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({ warehouse_ids: warehouseIds })
    }).then(function (data) {
      renderOzonWarehouseSettings(data);
      cache.overview = null;
      loadOverview(true);
      if (window.showWip) window.showWip("Склады Ozon сохранены");
    }).catch(function (error) {
      byId("fbs-ozon-warehouses-status").classList.add("error");
      byId("fbs-ozon-warehouses-status").textContent = error.message;
    }).finally(function () {
      ozonWarehouseSettingsSaving = false;
      save.textContent = "Сохранить";
      if (!byId("fbs-ozon-warehouses-status").classList.contains("error")) save.disabled = false;
    });
  }

  function orderQuery() {
    var group = byId("fbs-order-group").value || "all";
    var market = byId("fbs-order-marketplace").value || "";
    var search = byId("fbs-order-search").value || "";
    return "?page=" + orderPage + "&group=" + encodeURIComponent(group) +
      "&marketplace=" + encodeURIComponent(market) + "&search=" + encodeURIComponent(search);
  }
  function renderOrders(data) {
    orderPages = Number(data.pages || 1);
    orderPage = Number(data.page || 1);
    var body = byId("fbs-orders-body");
    body.innerHTML = data.results.length ? data.results.map(function (row) {
      return '<tr data-open="order"><td><strong>' + esc(row.external_order_id) + "</strong><small>" +
        esc(row.warehouse ? "Склад " + row.warehouse : "") + "</small></td><td>" + esc(row.marketplace_label) +
        "</td><td>" + pill(row.status_label, row.status_group) +
        '</td><td class="numeric">' + number(row.line_count) + '</td><td class="numeric">' + number(row.unit_count) +
        "</td><td>" + esc(row.cutoff_at || "-") + "</td><td>" + esc(row.updated_at || "-") +
        '</td><td><a class="fbs-back" href="#/fbs?tab=orders&order=' + encodeURIComponent(row.id) + '">Открыть</a></td></tr>';
    }).join("") : emptyRow(8, "Заказов по фильтру нет.");
    byId("fbs-orders-count").textContent = "Найдено: " + number(data.total) + " · страница " + orderPage + " из " + orderPages;
    byId("fbs-orders-prev").disabled = orderPage <= 1;
    byId("fbs-orders-next").disabled = orderPage >= orderPages;
  }
  function loadOrders() {
    byId("fbs-orders-body").innerHTML = emptyRow(8, "Загрузка...");
    return api("/client/api/v1/fbs/orders/" + orderQuery()).then(renderOrders).catch(fail);
  }
  function renderOrderDetail(data) {
    var rows = (data.items || []).map(function (item) {
      var requirements = [];
      if (item.requirements && item.requirements.marking_required) requirements.push("Честный знак");
      if (item.requirements && item.requirements.expiry_required) requirements.push("Срок годности");
      return "<tr><td><strong>" + esc(item.product_name || item.sku_code || item.external_sku) + "</strong><small>" +
        esc(item.sku_code || item.external_sku) + "</small></td><td>" + esc(item.barcode || "-") +
        '</td><td class="numeric">' + number(item.quantity) + "</td><td>" + esc(requirements.join(", ") || "-") + "</td></tr>";
    });
    byId("fbs-order-detail").innerHTML = '<div class="fbs-detail-head"><div><h2>Заказ ' + esc(data.external_order_id) +
      "</h2><p>" + esc(data.marketplace_label) + " · склад " + esc(data.warehouse || "-") +
      "</p></div>" + pill(data.status_label, data.status_group) + "</div>" +
      '<div class="fbs-detail-kpis"><div><span>Позиций</span><strong>' + number(data.line_count) +
      "</strong></div><div><span>Единиц</span><strong>" + number(data.unit_count) +
      "</strong></div><div><span>Срок сборки</span><strong>" + esc(data.cutoff_at || "-") +
      "</strong></div><div><span>Обновлен</span><strong>" + esc(data.updated_at || "-") + "</strong></div></div>" +
      detailTable(["Товар", "Штрихкод", "Количество", "Требования"], rows.length ? rows : [emptyRow(4, "Состав заказа пуст.")]);
  }
  function loadOrderDetail(id) {
    byId("fbs-order-detail").innerHTML = '<p class="fbs-empty">Загрузка...</p>';
    return api("/client/api/v1/fbs/orders/" + encodeURIComponent(id) + "/").then(renderOrderDetail).catch(fail);
  }

  function renderStock(data) {
    byId("fbs-stock-count").textContent = number(data.total) + " позиций";
    byId("fbs-stock-kpis").innerHTML = [
      kpi("FBS всего", number(data.summary.qty), "единиц", ""),
      kpi("Доступно", number(data.summary.available_qty), "к заказам", "accent"),
      kpi("Резерв по заказам", number(data.summary.reserved_qty), "единиц", ""),
      kpi("Ниже минимума", number(data.summary.needs_replenishment), "SKU", data.summary.needs_replenishment ? "attention" : "")
    ].join("");
    byId("fbs-stock-body").innerHTML = data.results.length ? data.results.map(function (row) {
      return '<tr data-open="stock"><td><strong>' + esc(row.name || row.sku_code || "Без названия") +
        "</strong><small>" + esc(row.sku_code || "-") + (row.size ? " · " + esc(row.size) : "") +
        "</small></td><td>" + esc(row.barcode || "-") + '</td><td class="numeric">' + number(row.qty) +
        '</td><td class="numeric">' + number(row.available_qty) + '</td><td class="numeric">' + number(row.reserved_qty) +
        '</td><td class="numeric">' + number(row.box_count) + '</td><td class="numeric">' + number(row.general_available_qty) +
        '</td><td>' + (row.barcode ? '<a class="fbs-back" href="#/fbs?tab=stock&barcode=' + encodeURIComponent(row.barcode) + '">Открыть</a>' : "-") + "</td></tr>";
    }).join("") : emptyRow(8, "FBS-остатков пока нет.");
  }
  function loadStock(force) {
    var search = byId("fbs-stock-search").value || "";
    var key = "stock:" + search;
    if (cache[key] && !force) { renderStock(cache[key]); return Promise.resolve(); }
    byId("fbs-stock-body").innerHTML = emptyRow(8, "Загрузка...");
    return api("/client/api/v1/fbs/stock/?search=" + encodeURIComponent(search)).then(function (data) {
      cache[key] = data; renderStock(data);
    }).catch(fail);
  }
  function renderStockDetail(data) {
    var fbsRows = (data.fbs_locations || []).map(function (row) {
      return "<tr><td>" + esc(row.cell) + "</td><td>" + esc(row.pallet) + "</td><td>" + esc(row.box) +
        '</td><td class="numeric">' + number(row.qty) + '</td><td class="numeric">' + number(row.available_qty) +
        '</td><td class="numeric">' + number(row.reserved_qty) + "</td><td>" + esc(row.expiry_date || "-") + "</td></tr>";
    });
    var commonRows = (data.general_locations || []).map(function (row) {
      return "<tr><td>" + esc(row.location || "-") + "</td><td>" + esc(row.container || "-") +
        '</td><td class="numeric">' + number(row.available_qty) + "</td><td>" + esc(row.lot_code || "-") +
        "</td><td>" + esc(row.expiry_date || "-") + "</td></tr>";
    });
    byId("fbs-stock-detail").innerHTML = '<div class="fbs-detail-head"><div><h2>' + esc(data.name || data.sku_code || data.barcode) +
      "</h2><p>" + esc(data.sku_code || "-") + " · ШК " + esc(data.barcode) + "</p></div></div>" +
      '<section class="fbs-surface"><header><h2>Размещение FBS</h2></header>' +
      detailTable(["Ячейка", "Паллета", "Короб", "Всего", "Доступно", "Резерв по заказам", "Срок годности"], fbsRows.length ? fbsRows : [emptyRow(7, "В FBS остатка нет.")]) +
      '</section><section class="fbs-surface"><header><h2>Общий склад</h2></header>' +
      detailTable(["Место", "Контейнер", "Доступно", "Партия", "Срок годности"], commonRows.length ? commonRows : [emptyRow(5, "В общем остатке товара нет.")]) + "</section>";
  }
  function loadStockDetail(barcode) {
    byId("fbs-stock-detail").innerHTML = '<p class="fbs-empty">Загрузка...</p>';
    return api("/client/api/v1/fbs/stock/" + encodeURIComponent(barcode) + "/").then(renderStockDetail).catch(fail);
  }

  function renderReports(data) {
    byId("fbs-report-kpis").innerHTML = [
      kpi("Заказов всего", number(data.summary.total), "за весь период", ""),
      kpi("Заказ поступил", number(data.summary.received), "заказов", ""),
      kpi("Товар подобран", number(data.summary.picked), "заказов", "accent"),
      kpi("Заказ отгружен", number(data.summary.shipped), "заказов", "")
    ].join("");
    byId("fbs-held-definition").textContent = "";
    var maxValue = Math.max.apply(Math, [1].concat((data.trend || []).map(function (row) { return row.orders; })));
    byId("fbs-report-trend").innerHTML = (data.trend || []).map(function (row) {
      var orderHeight = Math.max(Math.round((row.orders / maxValue) * 118), row.orders ? 4 : 2);
      return '<div class="fbs-trend-item"><div class="fbs-trend-bars"><i style="height:' + orderHeight + 'px" title="Заказов: ' + row.orders +
        '"></i></div><span>' + esc(row.date) + "<br>" + number(row.orders) + "</span></div>";
    }).join("");
    byId("fbs-report-marketplaces").innerHTML = (data.marketplaces || []).length ? data.marketplaces.map(function (row) {
      return '<div class="fbs-status-row"><span><strong>' + esc(row.label) + "</strong><small> Поступило " + number(row.received) +
        " · подобрано " + number(row.picked) + " · отгружено " + number(row.shipped) + "</small></span><strong>" + number(row.total) + "</strong></div>";
    }).join("") : '<p class="fbs-empty">Данных пока нет.</p>';
    byId("fbs-report-statuses").innerHTML = (data.statuses || []).length ? data.statuses.map(function (row) {
      return '<div class="fbs-status-row"><span>' + esc(row.label) + "</span><strong>" + number(row.count) + "</strong></div>";
    }).join("") : '<p class="fbs-empty">Данных пока нет.</p>';
  }
  function loadReports(force) {
    if (cache.reports && !force) { renderReports(cache.reports); return Promise.resolve(); }
    return api("/client/api/v1/fbs/reports/").then(function (data) { cache.reports = data; renderReports(data); }).catch(fail);
  }

  function renderMovements(data) {
    byId("fbs-movements-body").innerHTML = data.results.length ? data.results.map(function (row) {
      return "<tr><td><strong>" + esc(row.number) + "</strong><small>" + esc(row.source_file_name || "Вручную") +
        "</small></td><td>" + esc(row.mode_label) + "</td><td>" + pill(row.status_label, row.status) +
        '</td><td class="numeric">' + number(row.line_count) + '</td><td class="numeric">' + number(row.requested_qty) +
        (row.actual_moved_qty ? "<small>Перемещено: " + number(row.actual_moved_qty) + "</small>" : "") +
        '</td><td class="numeric">' + number(row.requested_box_count) +
        (row.requested_mixed_box_count ? "<small>Микс: " + number(row.requested_mixed_box_count) + "</small>" : "") +
        "</td><td>" + esc(row.created_at) + '</td><td><a class="fbs-back" href="#/fbs?tab=movements&movement=' +
        encodeURIComponent(row.id) + '">Открыть</a></td></tr>';
    }).join("") : emptyRow(8, "Заявок на перемещение пока нет.");
  }
  function loadMovements(force) {
    if (cache.movements && !force) { renderMovements(cache.movements); return Promise.resolve(); }
    return api("/client/api/v1/fbs/movements/").then(function (data) { cache.movements = data; renderMovements(data); }).catch(fail);
  }
  function renderMovementDetail(data) {
    var rows = (data.lines || []).map(function (line) {
      return "<tr><td><strong>" + esc(line.product_name || line.sku_code) + "</strong><small>" + esc(line.sku_code) +
        "</small></td><td>" + esc(line.barcode) + '</td><td class="numeric">' + number(line.requested_qty) +
        '</td><td class="numeric">' + (data.mode === "box"
          ? (Number(line.units_per_box) === 1 && Number(line.requested_qty) !== Number(line.requested_box_count)
            ? "разные / микс"
            : number(line.units_per_box))
          : "-") +
        '</td><td class="numeric">' + (data.mode === "box" ? number(line.requested_box_count) : "-") +
        '</td><td class="numeric">' + number(line.general_available_qty_snapshot) + '</td><td class="numeric">' +
        number(line.fbs_available_qty_snapshot) + "</td></tr>";
    });
    byId("fbs-movement-detail").innerHTML = '<div class="fbs-detail-head"><div><h2>' + esc(data.number) +
      "</h2><p>" + esc(data.mode_label) + " · создана " + esc(data.created_at) + "</p></div>" +
      pill(data.status_label, data.status) + '</div><div class="fbs-detail-kpis"><div><span>Позиций</span><strong>' +
      number(data.line_count) + "</strong></div><div><span>Запрошено</span><strong>" + number(data.requested_qty) +
      "</strong></div><div><span>Перемещено</span><strong>" + number(data.actual_moved_qty) +
      "</strong></div><div><span>Физических коробов</span><strong>" + number(data.actual_moved_box_count || data.actual_packed_box_count) + " / " + number(data.requested_box_count) +
      "</strong></div><div><span>Микс-коробов</span><strong>" + number(data.actual_mixed_box_count) + " / " + number(data.requested_mixed_box_count) +
      "</strong></div><div><span>Файл</span><strong>" + esc(data.source_file_name || "Вручную") + "</strong></div></div>" +
      (data.clarification_reason ? '<section class="fbs-surface"><header><h2>Уточнение по выполнению</h2></header><p>' + esc(data.clarification_reason) + "</p></section>" : "") +
      (data.comment ? '<section class="fbs-surface"><header><h2>Комментарий</h2></header><p>' + esc(data.comment) + "</p></section>" : "") +
      detailTable(["Товар", "Штрихкод", "Количество", "Кратность", "Коробов", "Доступно перед заявкой", "FBS на момент заявки"], rows.length ? rows : [emptyRow(7, "Состав заявки пуст.")]) +
      (data.can_cancel ? '<div class="fbs-actions"><button type="button" class="fbs-button fbs-button-danger" id="fbs-movement-cancel">Отменить заявку</button></div>' : "");
    var cancelButton = byId("fbs-movement-cancel");
    if (cancelButton) cancelButton.addEventListener("click", function () {
      if (!window.confirm("Отменить заявку и освободить зарезервированный товар?")) return;
      cancelButton.disabled = true;
      api("/client/api/v1/fbs/movements/" + encodeURIComponent(data.id) + "/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({ action: "cancel" })
      }).then(function (updated) {
        cache.movements = null;
        cache.overview = null;
        renderMovementDetail(updated);
      }).catch(function (error) {
        cancelButton.disabled = false;
        fail(error);
      });
    });
  }
  function loadMovementDetail(id) {
    byId("fbs-movement-detail").innerHTML = '<p class="fbs-empty">Загрузка...</p>';
    return api("/client/api/v1/fbs/movements/" + encodeURIComponent(id) + "/").then(renderMovementDetail).catch(fail);
  }

  function outboundErrors(error) {
    var box = byId("fbs-outbound-errors");
    var messages = error && error.details && error.details.length
      ? error.details
      : [error && error.message ? error.message : "Заявка на вывоз не создана."];
    box.innerHTML = messages.map(function (message) { return "<div>" + esc(message) + "</div>"; }).join("");
    box.hidden = false;
  }
  function clearOutboundErrors() {
    var box = byId("fbs-outbound-errors");
    box.hidden = true;
    box.innerHTML = "";
  }
  function outboundRouteSummary(row) {
    var shipping = row.shipping || {};
    var main = shipping.delivery_type === "marketplace"
      ? [shipping.marketplace_name, shipping.destination_warehouse].filter(Boolean).join(" · ")
      : "Самовывоз со склада Fullbox";
    var details = [
      shipping.eta_date ? "Дата: " + shipping.eta_date : "",
      shipping.vehicle_type_label || "",
      shipping.vehicle_number || ""
    ].filter(Boolean).join(" · ");
    return "<strong>" + esc(main || "-") + "</strong>" + (details ? "<small>" + esc(details) + "</small>" : "");
  }
  function renderOutboundDocuments(data) {
    var rows = data.results || [];
    byId("fbs-outbound-body").innerHTML = rows.length ? rows.map(function (row) {
      return "<tr><td><strong>" + esc(row.number) + "</strong></td><td>" + esc(row.delivery_type_label) +
        "</td><td>" + outboundRouteSummary(row) + "</td><td>" + pill(row.status_label, row.status) + '</td><td class="numeric">' + number(row.requested_qty) +
        '</td><td class="numeric">' + number(row.picked_qty) + '</td><td class="numeric">' + number(row.shipped_qty) +
        "</td><td>" + esc(row.created_at) + "</td></tr>";
    }).join("") : emptyRow(8, "Заявок на вывоз пока нет.");
  }
  function renderOutboundMarketplaces(data) {
    var select = byId("fbs-outbound-marketplace");
    var current = select.value;
    select.innerHTML = '<option value="">Выберите маркетплейс</option>' + (data.marketplaces || []).map(function (row) {
      return '<option value="' + esc(row.id) + '">' + esc(row.name) + "</option>";
    }).join("");
    if (current) select.value = current;
  }
  function outboundIsMarketplace() {
    return byId("fbs-outbound-delivery-type").value === "marketplace";
  }
  function updateOutboundDeliveryFields() {
    var marketplace = outboundIsMarketplace();
    var fields = byId("fbs-outbound-marketplace-fields");
    fields.style.display = marketplace ? "grid" : "none";
    [
      "fbs-outbound-marketplace",
      "fbs-outbound-shipping-barcode",
      "fbs-outbound-supply-number",
      "fbs-outbound-supply-type",
      "fbs-outbound-destination-warehouse"
    ].forEach(function (id) { byId(id).required = marketplace; });
    var today = new Date();
    var minimum = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, "0"),
      String(today.getDate()).padStart(2, "0")
    ].join("-");
    byId("fbs-outbound-eta-date").min = minimum;
    byId("fbs-outbound-slot-date").min = minimum;
  }
  function outboundShippingDetails() {
    return {
      delivery_type: byId("fbs-outbound-delivery-type").value || "pickup",
      marketplace_id: byId("fbs-outbound-marketplace").value || "",
      shipping_barcode: byId("fbs-outbound-shipping-barcode").value || "",
      supply_number: byId("fbs-outbound-supply-number").value || "",
      supply_type: byId("fbs-outbound-supply-type").value || "",
      destination_warehouse: byId("fbs-outbound-destination-warehouse").value || "",
      slot_date: byId("fbs-outbound-slot-date").value || "",
      slot_time: byId("fbs-outbound-slot-time").value || "",
      eta_date: byId("fbs-outbound-eta-date").value || "",
      vehicle_type: byId("fbs-outbound-vehicle-type").value || "",
      vehicle_number: byId("fbs-outbound-vehicle-number").value || "",
      driver_phone: byId("fbs-outbound-driver-phone").value || ""
    };
  }
  function renderOutboundStock(data) {
    outboundStock = (data.stock && data.stock.results) || [];
    var stock = data.stock || {};
    var count = byId("fbs-outbound-stock-count");
    count.textContent = "Свободно: " + number(stock.available_qty) + " шт. · строк: " + number(stock.total) +
      (stock.truncated ? " · уточните поиск" : "");
    byId("fbs-outbound-stock-body").innerHTML = outboundStock.length ? outboundStock.map(function (row) {
      var productMeta = [row.sku_code, row.size].filter(Boolean).join(" · ");
      var code = row.marking_code ? esc(row.barcode || "-") + "<small>ЧЗ: " + esc(row.marking_code) + "</small>" : esc(row.barcode || "-");
      var lot = [row.lot_code ? "Партия " + row.lot_code : "", row.expiry_date ? "до " + row.expiry_date : ""].filter(Boolean).join(" · ") || "-";
      return '<tr><td><strong>' + esc(row.name || row.sku_code) + "</strong><small>" + esc(productMeta) +
        "</small></td><td>" + code + "</td><td>" + esc(row.location || "-") + "<small>" + esc(row.box_code || "") +
        '</small></td><td>' + esc(lot) + '</td><td class="numeric"><strong>' + number(row.available_qty) +
        '</strong></td><td><input class="fbs-outbound-qty" type="number" inputmode="numeric" min="0" max="' +
        esc(row.available_qty) + '" step="1" value="' + esc(row.selected_qty || 0) + '" data-balance-id="' + esc(row.balance_id) +
        '" aria-label="Количество к вывозу ' + esc(row.name || row.sku_code) + '"></td></tr>';
    }).join("") : emptyRow(6, data.can_create ? "Свободного товара по фильтру нет." : "Для создания заявки нужен доступ к разделу «Заявки».");
    var submit = byId("fbs-outbound-submit");
    submit.disabled = !data.can_create || outboundSubmitting;
    if (!data.can_create) outboundErrors({ details: ["Создать заявку может владелец или сотрудник клиента с доступом к разделу «Заявки»."] });
    updateOutboundSelection();
  }
  function renderOutbound(data) {
    cache.outbound = data;
    renderOutboundMarketplaces(data);
    renderOutboundDocuments(data);
    renderOutboundStock(data);
    updateOutboundDeliveryFields();
  }
  function importOutboundFile(file) {
    if (!file || outboundImporting) return;
    clearOutboundErrors();
    outboundImporting = true;
    var form = new FormData();
    form.append("file", file);
    byId("fbs-outbound-file-name").textContent = "Проверка файла...";
    api("/client/api/v1/fbs/outbound/import/", {
      method: "POST",
      headers: { "X-CSRFToken": csrfToken() },
      body: form
    }).then(function (data) {
      renderOutboundStock(data);
      byId("fbs-outbound-file-name").textContent = (data.filename || file.name || "Файл загружен") +
        " · ШК: " + number((data.matched || []).length) + " · выбрано: " + number(data.requested_qty) + " шт.";
    }).catch(function (error) {
      byId("fbs-outbound-file-name").textContent = file.name || "Файл не прошёл проверку";
      outboundErrors(error);
    }).finally(function () {
      outboundImporting = false;
    });
  }
  function loadOutbound(force) {
    var search = byId("fbs-outbound-search").value || "";
    if (cache.outbound && !force && !search) { renderOutbound(cache.outbound); return Promise.resolve(); }
    byId("fbs-outbound-stock-body").innerHTML = emptyRow(6, "Загрузка...");
    return api("/client/api/v1/fbs/outbound/?search=" + encodeURIComponent(search)).then(renderOutbound).catch(function (error) {
      outboundErrors(error);
      fail(error);
    });
  }
  function updateOutboundSelection() {
    var total = 0;
    root.querySelectorAll(".fbs-outbound-qty").forEach(function (input) {
      var qty = Number(input.value || 0);
      if (Number.isInteger(qty) && qty > 0) total += qty;
    });
    byId("fbs-outbound-selection").textContent = "Выбрано: " + number(total) + " шт.";
  }
  function submitOutbound(event) {
    event.preventDefault();
    clearOutboundErrors();
    var selections = [];
    var errors = [];
    var shippingDetails = outboundShippingDetails();
    if (!shippingDetails.eta_date) errors.push("Укажите дату отгрузки.");
    if (!shippingDetails.vehicle_type) errors.push("Выберите транспорт.");
    if (shippingDetails.delivery_type === "marketplace") {
      if (!shippingDetails.marketplace_id) errors.push("Выберите маркетплейс.");
      if (!shippingDetails.shipping_barcode.trim()) errors.push("Укажите ШК поставки.");
      if (!shippingDetails.supply_number.trim()) errors.push("Укажите номер поставки.");
      if (!shippingDetails.supply_type) errors.push("Выберите тип поставки.");
      if (!shippingDetails.destination_warehouse.trim()) errors.push("Укажите склад назначения.");
    }
    root.querySelectorAll(".fbs-outbound-qty").forEach(function (input) {
      var qty = Number(input.value || 0);
      var maximum = Number(input.max || 0);
      if (!qty) return;
      if (!Number.isInteger(qty) || qty < 1 || qty > maximum) {
        errors.push("Количество к вывозу должно быть целым и не больше свободного остатка.");
        return;
      }
      selections.push({ balance_id: input.getAttribute("data-balance-id"), qty: qty });
    });
    if (!selections.length) errors.push("Укажите количество хотя бы у одного товара.");
    if (errors.length) { outboundErrors({ details: errors }); return; }
    outboundSubmitting = true;
    var submit = byId("fbs-outbound-submit");
    submit.disabled = true;
    submit.textContent = "Отправляем...";
    api("/client/api/v1/fbs/outbound/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({
        basis: byId("fbs-outbound-basis").value || "",
        shipping_details: shippingDetails,
        selections: selections,
        idempotency_key: outboundIdempotencyKey
      })
    }).then(function (data) {
      cache.outbound = null;
      cache.overview = null;
      outboundIdempotencyKey = newOutboundIdempotencyKey();
      byId("fbs-outbound-form").reset();
      byId("fbs-outbound-file-name").textContent = "Файл не выбран";
      updateOutboundDeliveryFields();
      location.hash = "#/fbs?tab=outbound";
      if (window.showWip) window.showWip("Заявка " + data.number + " отправлена на склад");
    }).catch(outboundErrors).finally(function () {
      outboundSubmitting = false;
      submit.textContent = "Отправить заявку";
      submit.disabled = false;
    });
  }

  function movementMode() {
    var checked = root.querySelector('input[name="fbs_mode"]:checked');
    return checked ? checked.value : "item";
  }
  function movementRequestBoxPlan() {
    return {
      boxCount: Number(byId("fbs-movement-request-box-count").value || 0),
      mixedBoxCount: Number(byId("fbs-movement-mixed-box-count").value || 0)
    };
  }
  function setMovementDraftStatus(message) {
    var status = byId("fbs-movement-draft-status");
    if (status) status.textContent = message;
  }
  function updateMovementSubmitState() {
    var button = byId("fbs-movement-submit");
    if (!button) return;
    button.disabled = movementSubmitting || movementStockLoading || !movementStockLoaded;
    if (movementStockLoading) button.title = "Дождитесь обновления актуальных остатков.";
    else if (!movementStockLoaded) button.title = "Сначала загрузите актуальные остатки.";
    else button.removeAttribute("title");
  }
  function clearMovementDraft() {
    try { window.sessionStorage.removeItem(movementDraftKey); } catch (error) { /* Storage can be unavailable. */ }
  }
  function saveMovementDraft() {
    var rows = movementRows();
    var comment = byId("fbs-movement-comment").value || "";
    if (!rows.length && !selectedMovementMixedBoxCodes.length && !comment && !sourceFileName) {
      clearMovementDraft();
      setMovementDraftStatus("Товары и короба сохраняются автоматически до отправки заявки.");
      return;
    }
    try {
      window.sessionStorage.setItem(movementDraftKey, JSON.stringify({
        version: MOVEMENT_DRAFT_VERSION,
        mode: movementMode(),
        rows: rows,
        mixed_box_codes: selectedMovementMixedBoxCodes.slice(),
        requested_box_count: movementRequestBoxPlan().boxCount,
        requested_mixed_box_count: movementRequestBoxPlan().mixedBoxCount,
        idempotency_key: movementIdempotencyKey,
        comment: comment,
        source_file_name: sourceFileName
      }));
      setMovementDraftStatus("Черновик сохранен автоматически.");
    } catch (error) {
      setMovementDraftStatus("Не удалось сохранить черновик в браузере.");
    }
  }
  function restoreMovementDraft() {
    var draft;
    try { draft = JSON.parse(window.sessionStorage.getItem(movementDraftKey) || "null"); }
    catch (error) { clearMovementDraft(); return false; }
    if (!draft || [1, 2, 3, 4, 5, 6, 7, 8].indexOf(draft.version) < 0 || !Array.isArray(draft.rows)) return false;
    var restoredMixedBoxCodes = Array.isArray(draft.mixed_box_codes)
      ? draft.mixed_box_codes.map(function (value) { return String(value || "").trim(); }).filter(Boolean)
      : [];
    if (!draft.rows.length && !restoredMixedBoxCodes.length) return false;
    var mode = draft.mode === "box" ? "box" : "item";
    if (mode === "box" && draft.version < 5) {
      clearMovementDraft();
      setMovementDraftStatus("Старый черновик коробов сброшен: обновились доступные короба и их кратность.");
      return false;
    }
    if (draft.version === 1 && mode === "box") {
      draft.rows = draft.rows.map(function (row) {
        var qty = Number(row.qty || 0);
        var unitsPerBox = Number(row.units_per_box || 0);
        var boxCount = unitsPerBox > 0 && qty > 0 && qty % unitsPerBox === 0
          ? qty / unitsPerBox
          : Number(row.box_count || 0);
        return {
          row_number: row.row_number,
          barcode: row.barcode,
          qty: row.qty,
          units_per_box: row.units_per_box,
          box_count: boxCount || ""
        };
      });
    }
    var radio = root.querySelector('input[name="fbs_mode"][value="' + mode + '"]');
    if (radio) radio.checked = true;
    var restoredBoxCount = Number(draft.requested_box_count || 0);
    if (draft.version < 3 && mode === "item") {
      restoredBoxCount = draft.rows.reduce(function (sum, row) {
        return sum + (Number(row.qty || 0) > 0 ? Math.max(Number(row.box_count || 1), 1) : 0);
      }, 0);
    }
    byId("fbs-movement-request-box-count").value = String(Math.max(restoredBoxCount || 1, 1));
    byId("fbs-movement-mixed-box-count").value = String(Math.max(Number(draft.requested_mixed_box_count || 0), 0));
    sourceFileName = String(draft.source_file_name || "");
    byId("fbs-movement-file-name").textContent = sourceFileName || "Файл не выбран";
    byId("fbs-movement-comment").value = String(draft.comment || "");
    movementIdempotencyKey = String(draft.idempotency_key || "") || newMovementIdempotencyKey();
    selectedMovementMixedBoxCodes = mode === "box" ? restoredMixedBoxCodes : [];
    renderMovementRows(draft.rows.length ? draft.rows : [{}]);
    setMovementDraftStatus("Черновик восстановлен. Товары и план коробов сохранены.");
    return true;
  }
  function movementProductHtml(row) {
    if (!row || !(row.sku_code || row.name || row.product_name)) {
      return '<span class="fbs-product-pending">Определится по ШК</span>';
    }
    return "<strong>" + esc(row.sku_code || "-") + "</strong><span>" +
      esc(row.name || row.product_name || "-") + "</span>" +
      (row.brand ? "<small>" + esc(row.brand) + "</small>" : "");
  }
  function applyMovementStockToLine(tr, row) {
    if (!tr) return;
    var productCell = tr.querySelector('[data-value="product"]');
    var goodsTypeCell = tr.querySelector('[data-value="goods_type"]');
    var availableCell = tr.querySelector('[data-value="client_available"]');
    var qtyInput = tr.querySelector('[data-field="qty"]');
    var unitsInput = tr.querySelector('[data-field="units_per_box"]');
    if (productCell) productCell.innerHTML = movementProductHtml(row);
    if (row && row.barcode) tr.setAttribute("data-committed-barcode", String(row.barcode));
    if (goodsTypeCell) goodsTypeCell.textContent = row ? (row.goods_type || "Готовый") : "-";
    if (availableCell) availableCell.textContent = row ? number(row.client_available_qty) : "-";
    if (qtyInput) {
      if (row) qtyInput.max = String(Number(row.client_available_qty || 0));
      else qtyInput.removeAttribute("max");
    }
    if (unitsInput && movementMode() === "box") syncMovementLineCalculation(tr, false);
  }
  function findMovementLine(barcode) {
    var normalized = String(barcode || "").trim();
    return Array.prototype.find.call(byId("fbs-movement-lines").querySelectorAll("tr"), function (tr) {
      return tr.querySelector('[data-field="barcode"]').value.trim() === normalized;
    });
  }
  function movementLineValues(barcode) {
    var line = findMovementLine(barcode);
    if (!line) return { requestedQty: 0, qty: 0, unitsPerBox: 1, boxCount: 0, boxPlan: [], remainder: 0 };
    var requestedQty = Number(line.querySelector('[data-field="qty"]').value || 0);
    if (movementMode() === "item") {
      return { requestedQty: requestedQty, qty: requestedQty, unitsPerBox: 1, boxCount: 0, boxPlan: [], remainder: 0 };
    }
    var plan = movementBoxPlan(movementStockByBarcode[barcode], requestedQty);
    return {
      requestedQty: requestedQty,
      qty: plan.totalQty,
      unitsPerBox: plan.items.length === 1 ? plan.items[0].units_per_box : 1,
      boxCount: plan.boxCount,
      boxPlan: plan.items,
      remainder: plan.remainder
    };
  }
  function selectedMovementMixedBoxes() {
    return selectedMovementMixedBoxCodes.map(function (code) {
      return movementMixedBoxByCode[code];
    }).filter(Boolean);
  }
  function setMovementMixedBoxSelected(code, selected) {
    var normalized = String(code || "").trim();
    if (!normalized) return;
    var index = selectedMovementMixedBoxCodes.indexOf(normalized);
    if (selected && index < 0) selectedMovementMixedBoxCodes.push(normalized);
    else if (!selected && index >= 0) selectedMovementMixedBoxCodes.splice(index, 1);
  }
  function renderMovementMixedBoxSelection() {
    var section = byId("fbs-movement-mixed-selection");
    var cards = byId("fbs-movement-mixed-selection-cards");
    var count = byId("fbs-movement-mixed-selection-count");
    if (!section || !cards || !count) return;
    var boxes = selectedMovementMixedBoxes();
    section.hidden = movementMode() !== "box" || !boxes.length;
    count.textContent = boxes.length
      ? "Выбрано физических коробов: " + number(boxes.length)
      : "Микс-короба пока не выбраны.";
    cards.innerHTML = boxes.map(function (box) {
      var items = (box.items || []).map(function (item) {
        return '<li><span><strong>' + esc(item.sku_code || item.barcode) + '</strong><small>' +
          esc(item.name || "-") + ' · ШК ' + esc(item.barcode || "-") +
          '</small></span><b>' + number(item.qty) + ' шт.</b></li>';
      }).join("");
      return '<article class="fbs-mixed-selection-card"><div class="fbs-mixed-selection-card-head"><div>' +
        '<strong>' + esc(box.container_code) + '</strong><span>' + number(box.total_qty) +
        ' шт. · ' + number(box.position_count) + ' поз.</span></div><button type="button" class="ghost" ' +
        'data-remove-mixed-box="' + esc(box.container_code) + '">Убрать короб</button></div>' +
        '<p>Переместится весь физический короб — все позиции ниже уже включены в заявку.</p>' +
        '<ul>' + items + '</ul></article>';
    }).join("");
  }
  function syncMovementMixedBoxPicker() {
    var picker = byId("fbs-movement-mixed-box-picker");
    if (!picker) return;
    var itemMode = movementMode() === "item";
    picker.hidden = itemMode;
    picker.querySelectorAll("[data-mixed-box-code]").forEach(function (card) {
      var code = card.getAttribute("data-mixed-box-code") || "";
      var checked = selectedMovementMixedBoxCodes.indexOf(code) >= 0;
      card.querySelectorAll("[data-mixed-box-control]").forEach(function (input) {
        input.checked = checked;
      });
      card.querySelectorAll("[data-mixed-box-item]").forEach(function (item) {
        item.classList.toggle("is-selected", checked);
      });
      card.querySelectorAll("[data-mixed-box-selected-qty]").forEach(function (value) {
        value.textContent = checked ? number(value.getAttribute("data-item-qty")) + " шт." : "—";
      });
      card.classList.toggle("is-selected", checked);
    });
    var count = byId("fbs-movement-existing-mixed-box-count");
    if (count) {
      count.textContent = "Доступно: " + number(Object.keys(movementMixedBoxByCode).length) +
        " · выбрано: " + number(selectedMovementMixedBoxCodes.length);
    }
    renderMovementMixedBoxSelection();
  }
  function renderMovementMixedBoxes(data, merge) {
    if (!merge) movementMixedBoxByCode = {};
    (data.mixed_boxes || []).forEach(function (box) {
      movementMixedBoxByCode[box.container_code] = box;
    });
    if (!merge) {
      selectedMovementMixedBoxCodes = selectedMovementMixedBoxCodes.filter(function (code) {
        return Boolean(movementMixedBoxByCode[code]);
      });
    }
    var grid = byId("fbs-movement-mixed-box-grid");
    if (!grid) return;
    var boxes = data.mixed_boxes || [];
    grid.innerHTML = boxes.length ? boxes.map(function (box) {
      var selected = selectedMovementMixedBoxCodes.indexOf(box.container_code) >= 0;
      var items = (box.items || []).map(function (item) {
        var label = item.sku_code || item.barcode || "Позиция";
        return '<label class="fbs-mixed-box-item ' + (selected ? "is-selected" : "") +
          '" data-mixed-box-item><input type="checkbox" data-mixed-box-control ' +
          (selected ? "checked " : "") + 'aria-label="Выбрать весь микс-короб ' +
          esc(box.container_code) + ' через позицию ' + esc(label) + '"><span class="fbs-mixed-box-item-main"><strong>' +
          esc(label) + '</strong><small>' + esc(item.name || "-") + ' · ШК ' +
          esc(item.barcode || "-") + '</small></span><span class="fbs-mixed-box-item-qty"><b>' +
          number(item.qty) + ' шт.</b><small>к перемещению: <strong data-mixed-box-selected-qty data-item-qty="' +
          Number(item.qty || 0) + '">' + (selected ? number(item.qty) + " шт." : "—") +
          '</strong></small></span></label>';
      }).join("");
      return '<article class="fbs-mixed-box-card ' + (selected ? "is-selected" : "") +
        '" data-mixed-box-code="' + esc(box.container_code) + '"><div class="fbs-mixed-box-card-head">' +
        '<label class="fbs-mixed-box-master"><input type="checkbox" data-mixed-box-control ' +
        (selected ? "checked " : "") + 'aria-label="Выбрать микс-короб ' + esc(box.container_code) +
        '"><span><strong>' + esc(box.container_code) + '</strong><small>Физический микс-короб</small></span></label>' +
        '<span class="fbs-mixed-box-tag">' + number(box.total_qty) + ' шт. · ' +
        number(box.position_count) + ' поз.</span></div><p class="fbs-mixed-box-callout">' +
        'Выберите любую позицию — система автоматически включит весь состав этого короба.</p>' +
        '<div class="fbs-mixed-box-items" role="group" aria-label="Состав короба ' +
        esc(box.container_code) + '">' + items + '</div></article>';
    }).join("") : '<p class="fbs-empty">Доступных физических микс-коробов по фильтру нет.</p>';
    syncMovementMixedBoxPicker();
  }
  function calculatedBoxQuantity(unitsPerBox, boxCount) {
    var units = Number(unitsPerBox || 0);
    var boxes = Number(boxCount || 0);
    return units > 0 && boxes > 0 ? units * boxes : 0;
  }
  function movementBoxOptions(row) {
    return (row && Array.isArray(row.box_options) ? row.box_options : []).filter(function (option) {
      return Number(option.units_per_box || 0) > 0;
    });
  }
  function selectableMovementBoxOptions(row) {
    return movementBoxOptions(row).filter(function (option) {
      return Number(option.available_box_count || 0) > 0;
    });
  }
  function greatestCommonDivisor(a, b) {
    a = Math.abs(Number(a || 0));
    b = Math.abs(Number(b || 0));
    while (b) {
      var next = a % b;
      a = b;
      b = next;
    }
    return a || 1;
  }
  function movementAllBoxPlan(row) {
    return movementBoxPlan(row, Number(row && row.client_available_qty || 0));
  }
  function movementBoxPlan(row, requestedQty) {
    var requested = Math.max(Math.floor(Number(requestedQty || 0)), 0);
    var options = selectableMovementBoxOptions(row).map(function (option) {
      return {
        units: Math.floor(Number(option.units_per_box || 0)),
        count: Math.floor(Number(option.available_box_count || 0))
      };
    }).filter(function (option) { return option.units > 0 && option.count > 0; })
      .sort(function (a, b) { return b.units - a.units; });
    if (!requested || !options.length) {
      return { requestedQty: requested, totalQty: 0, boxCount: 0, remainder: requested, items: [] };
    }
    var availableQty = options.reduce(function (sum, option) { return sum + option.units * option.count; }, 0);
    var target = Math.min(requested, availableQty);
    var divisor = options.reduce(function (value, option) {
      return greatestCommonDivisor(value, option.units);
    }, options[0].units);
    var scaledTarget = Math.floor(target / divisor);
    var planCounts = options.map(function () { return 0; });
    if (scaledTarget <= 500000) {
      var chunks = [];
      options.forEach(function (option, optionIndex) {
        var left = option.count;
        var chunkSize = 1;
        while (left > 0) {
          var boxes = Math.min(chunkSize, left);
          chunks.push({ optionIndex: optionIndex, boxes: boxes, qty: boxes * option.units / divisor });
          left -= boxes;
          chunkSize *= 2;
        }
      });
      var previous = new Int32Array(scaledTarget + 1);
      previous.fill(-1);
      previous[0] = -2;
      chunks.forEach(function (chunk, chunkIndex) {
        for (var amount = scaledTarget; amount >= chunk.qty; amount -= 1) {
          if (previous[amount] === -1 && previous[amount - chunk.qty] !== -1) previous[amount] = chunkIndex;
        }
      });
      var best = scaledTarget;
      while (best > 0 && previous[best] === -1) best -= 1;
      while (best > 0) {
        var usedChunk = chunks[previous[best]];
        planCounts[usedChunk.optionIndex] += usedChunk.boxes;
        best -= usedChunk.qty;
      }
    } else {
      var remaining = target;
      options.forEach(function (option, optionIndex) {
        var count = Math.min(option.count, Math.floor(remaining / option.units));
        planCounts[optionIndex] = count;
        remaining -= count * option.units;
      });
    }
    var items = options.map(function (option, optionIndex) {
      return { units_per_box: option.units, box_count: planCounts[optionIndex] };
    }).filter(function (item) { return item.box_count > 0; });
    var totalQty = items.reduce(function (sum, item) { return sum + item.units_per_box * item.box_count; }, 0);
    return {
      requestedQty: requested,
      totalQty: totalQty,
      boxCount: items.reduce(function (sum, item) { return sum + item.box_count; }, 0),
      remainder: Math.max(requested - totalQty, 0),
      items: items
    };
  }
  function boxPlanText(plan) {
    return (plan && plan.items || []).map(function (item) {
      return number(item.box_count) + " кор. × " + number(item.units_per_box) + " шт.";
    }).join(" + ");
  }
  function availableBoxesHtml(row) {
    var options = selectableMovementBoxOptions(row);
    if (!options.length) return '<span class="fbs-box-plan-empty">Свободных целых монокоробов нет</span>';
    var allPlan = movementAllBoxPlan(row);
    return '<div class="fbs-box-availability-list">' + options.map(function (option) {
      return '<span><strong>' + number(option.available_box_count) + ' кор.</strong> × ' +
        number(option.units_per_box) + ' шт.</span>';
    }).join("") + '</div><small>Можно выбрать одновременно: ' + number(allPlan.boxCount) +
      ' кор. · ' + number(allPlan.totalQty) + ' шт.</small>';
  }
  function boxPlanHtml(row, plan) {
    if (!plan || !plan.boxCount) return '<span class="fbs-box-plan-empty">—</span>';
    return '<div class="fbs-box-plan-result"><strong>' + esc(boxPlanText(plan)) + '</strong><small>' +
      number(plan.boxCount) + ' кор. · ' + number(plan.totalQty) + ' шт.</small></div>';
  }
  function boxPlanWarning(row, plan) {
    if (!plan || !plan.requestedQty || !plan.remainder) return "";
    var message = "По целым монокоробам получится " + number(plan.totalQty) + " шт.; " +
      number(plan.remainder) + " шт. не войдёт в эту часть заявки.";
    if (Number(row && row.mixed_box_available_qty || 0) > 0) {
      message += " Товар есть в физическом микс-коробе выше — его можно выбрать целиком.";
    } else if (Number(row && row.non_box_available_qty || 0) > 0) {
      message += " Остаток доступен только поштучно.";
    }
    return message;
  }
  function movementBoxOptionsHtml(row, selectedUnits) {
    var selected = Number(selectedUnits || 0);
    var options = movementBoxOptions(row);
    var available = selectableMovementBoxOptions(row);
    var html = '<option value="">Выберите целый короб</option>';
    options.forEach(function (option) {
      var units = Number(option.units_per_box || 0);
      var availableCount = Number(option.available_box_count || 0);
      var physicalCount = Number(option.physical_box_count || 0);
      var label = number(units) + " шт. в коробе · доступно " + number(availableCount);
      if (physicalCount !== availableCount) label += " из " + number(physicalCount);
      html += '<option value="' + units + '" data-available-box-count="' + availableCount + '"' +
        (availableCount <= 0 ? " disabled" : "") +
        (selected === units && availableCount > 0 ? " selected" : "") + ">" + esc(label) + "</option>";
    });
    if (!selected && available.length === 1) {
      html = html.replace(
        'value="' + Number(available[0].units_per_box) + '" data-available-box-count=',
        'value="' + Number(available[0].units_per_box) + '" selected data-available-box-count='
      );
    }
    return html;
  }
  function setMovementBoxOptions(select, row, selectedUnits) {
    if (!select) return;
    select.innerHTML = movementBoxOptionsHtml(row, selectedUnits);
    select.disabled = selectableMovementBoxOptions(row).length === 0;
  }
  function selectedBoxLimit(select) {
    var option = select && select.options ? select.options[select.selectedIndex] : null;
    return option ? Number(option.getAttribute("data-available-box-count") || 0) : 0;
  }
  function syncMovementLineCalculation(tr, deriveBoxMultiple) {
    if (!tr) return;
    var itemMode = movementMode() === "item";
    var qtyInput = tr.querySelector('[data-field="qty"]');
    var unitsInput = tr.querySelector('[data-field="units_per_box"]');
    var boxesInput = tr.querySelector('[data-field="box_count"]');
    if (!qtyInput || !unitsInput || !boxesInput) return;
    if (itemMode) {
      qtyInput.readOnly = false;
      qtyInput.placeholder = "шт.";
      qtyInput.classList.remove("is-calculated");
      unitsInput.innerHTML = '<option value="1" selected>1</option>';
      unitsInput.disabled = true;
      unitsInput.required = false;
      boxesInput.value = "";
      boxesInput.disabled = true;
      boxesInput.required = false;
      return;
    }
    qtyInput.readOnly = false;
    qtyInput.placeholder = "например, 1000";
    qtyInput.classList.remove("is-calculated");
    unitsInput.hidden = true;
    unitsInput.disabled = true;
    unitsInput.required = false;
    boxesInput.hidden = true;
    boxesInput.disabled = true;
    boxesInput.required = false;
    var barcodeInput = tr.querySelector('[data-field="barcode"]');
    var stockRow = movementStockByBarcode[barcodeInput ? barcodeInput.value.trim() : ""];
    var plan = movementBoxPlan(stockRow, qtyInput.value);
    unitsInput.value = plan.items.length === 1 ? String(plan.items[0].units_per_box) : "1";
    boxesInput.value = plan.boxCount ? String(plan.boxCount) : "";
    var planCell = tr.querySelector("[data-box-plan-result]");
    var boxCountValue = tr.querySelector("[data-box-count-value]");
    var warning = tr.querySelector("[data-box-warning]");
    if (planCell) planCell.innerHTML = boxPlanHtml(stockRow, plan);
    if (boxCountValue) boxCountValue.textContent = plan.boxCount ? number(plan.boxCount) : "—";
    if (warning) warning.textContent = boxPlanWarning(stockRow, plan);
  }
  function selectMovementStock(row, qty, unitsPerBox, boxCount, focusLine) {
    var existing = findMovementLine(row.barcode);
    if (existing) {
      applyMovementStockToLine(existing, row);
      existing.querySelector('[data-field="qty"]').value = String(Math.max(Number(qty || 0), 0) || "");
      syncMovementLineCalculation(existing, false);
      updateMovementTotal();
      if (focusLine) existing.querySelector('[data-field="qty"]').focus();
      return;
    }
    if (Number(qty || 0) <= 0) return;
    var blank = Array.prototype.find.call(byId("fbs-movement-lines").querySelectorAll("tr"), function (tr) {
      return !tr.querySelector('[data-field="barcode"]').value.trim() && !tr.querySelector('[data-field="qty"]').value;
    });
    if (blank) blank.remove();
    addMovementRow({
      barcode: row.barcode,
      sku_code: row.sku_code,
      product_name: row.name || row.product_name,
      brand: row.brand,
      goods_type: row.goods_type || "Готовый",
      qty: Math.max(Number(qty || 0), 1),
      units_per_box: 1,
      box_count: 0,
      general_available_qty: row.client_available_qty,
      fbs_available_qty: row.fbs_available_qty,
      box_options: row.box_options || []
    });
    var added = findMovementLine(row.barcode);
    if (added) applyMovementStockToLine(added, row);
    if (added && focusLine) added.querySelector('[data-field="qty"]').focus();
  }
  function removeMovementStockSelection(barcode) {
    clearPendingMovementQtyCommit(barcode);
    var line = findMovementLine(barcode);
    if (line) line.remove();
    if (!byId("fbs-movement-lines").children.length) addMovementRow({});
    updateMovementTotal();
  }
  function clearPendingMovementQtyCommit(barcode) {
    var normalized = String(barcode || "").trim();
    var pending = pendingMovementQtyCommits[normalized];
    if (!pending) return;
    window.clearTimeout(pending.timerId);
    if (pending.input) pending.input.removeAttribute("data-qty-pending");
    delete pendingMovementQtyCommits[normalized];
  }
  function commitMovementStockQuantity(input) {
    var tr = input ? input.closest("tr[data-barcode]") : null;
    var barcode = tr ? tr.getAttribute("data-barcode") : "";
    var row = movementStockByBarcode[barcode];
    clearPendingMovementQtyCommit(barcode);
    if (!row) return;
    var qty = Number(input.value || 0);
    if (qty > 0) selectMovementStock(row, qty, 1, 0, false);
    else removeMovementStockSelection(barcode);
  }
  function scheduleMovementStockQuantity(input) {
    var tr = input ? input.closest("tr[data-barcode]") : null;
    var barcode = tr ? tr.getAttribute("data-barcode") : "";
    if (!barcode || !movementStockByBarcode[barcode]) return;
    clearPendingMovementQtyCommit(barcode);
    input.setAttribute("data-qty-pending", "1");
    pendingMovementQtyCommits[barcode] = {
      input: input,
      timerId: window.setTimeout(function () {
        commitMovementStockQuantity(input);
      }, MOVEMENT_QTY_COMMIT_DELAY_MS)
    };
  }
  function flushPendingMovementQtyCommits() {
    Object.keys(pendingMovementQtyCommits).forEach(function (barcode) {
      var pending = pendingMovementQtyCommits[barcode];
      if (pending && pending.input && pending.input.isConnected) commitMovementStockQuantity(pending.input);
      else clearPendingMovementQtyCommit(barcode);
    });
  }
  function clearPendingMovementQtyCommits() {
    Object.keys(pendingMovementQtyCommits).forEach(clearPendingMovementQtyCommit);
  }
  function movementStockRowValue(tr, key, type) {
    var raw = tr.getAttribute("data-" + key.replace(/_/g, "-")) || "";
    return type === "number" ? Number(raw || 0) : String(raw).toLocaleLowerCase("ru-RU");
  }
  function applyMovementStockGrid() {
    var body = byId("fbs-movement-stock-body");
    var rows = Array.prototype.slice.call(body.querySelectorAll("tr[data-barcode]"));
    var filters = Array.prototype.slice.call(root.querySelectorAll(".fbs-stock-filter"));
    rows.sort(function (a, b) {
      var aSelected = a.classList.contains("is-selected");
      var bSelected = b.classList.contains("is-selected");
      if (aSelected !== bSelected) return aSelected ? -1 : 1;
      if (movementStockSort.key) {
        var aValue = movementStockRowValue(a, movementStockSort.key, movementStockSort.type);
        var bValue = movementStockRowValue(b, movementStockSort.key, movementStockSort.type);
        var compared = movementStockSort.type === "number"
          ? aValue - bValue
          : aValue.localeCompare(bValue, "ru", { sensitivity: "base" });
        if (compared) return movementStockSort.dir === "asc" ? compared : -compared;
      }
      return Number(a.getAttribute("data-source-index") || 0) - Number(b.getAttribute("data-source-index") || 0);
    });
    rows.forEach(function (tr) {
      body.appendChild(tr);
      var visible = filters.every(function (filter) {
        var needle = String(filter.value || "").trim().toLocaleLowerCase("ru-RU");
        if (!needle) return true;
        return String(tr.getAttribute("data-" + filter.getAttribute("data-filter-key")) || "")
          .toLocaleLowerCase("ru-RU").indexOf(needle) >= 0;
      });
      tr.hidden = !visible;
    });
  }
  function syncMovementStockPicker() {
    var itemMode = movementMode() === "item";
    byId("fbs-movement-stock-body").querySelectorAll("tr[data-barcode]").forEach(function (tr) {
      tr.querySelectorAll("[data-box-line-column]").forEach(function (cell) {
        cell.hidden = itemMode;
      });
      var barcode = tr.getAttribute("data-barcode");
      var selected = movementLineValues(barcode);
      var qtyInput = tr.querySelector(".fbs-move-qty");
      var planCell = tr.querySelector("[data-box-plan]");
      var boxesCell = tr.querySelector("[data-box-count]");
      var stockRow = movementStockByBarcode[barcode];
      if (qtyInput && document.activeElement !== qtyInput) qtyInput.value = selected.requestedQty > 0 ? String(selected.requestedQty) : "";
      if (qtyInput) {
        qtyInput.readOnly = false;
        qtyInput.placeholder = itemMode ? "шт." : "например, 1000";
      }
      var exceedsAvailable = selected.qty > Number(tr.getAttribute("data-client-available-qty") || 0);
      if (planCell && !itemMode) planCell.innerHTML = availableBoxesHtml(stockRow);
      if (boxesCell) {
        boxesCell.innerHTML = !itemMode
          ? boxPlanHtml(stockRow, selected) + '<small data-box-warning>' + esc(boxPlanWarning(stockRow, selected)) + '</small>'
          : "";
      }
      var warning = tr.querySelector("[data-box-warning]");
      if (warning) {
        if (!itemMode && !selectableMovementBoxOptions(stockRow).length) {
          warning.textContent = movementBoxOptions(stockRow).length
            ? "Целые короба заняты открытыми заявками."
            : "Свободных целых коробов нет.";
        } else {
          warning.textContent = exceedsAvailable
            ? "Недостаточно: доступно " + number(tr.getAttribute("data-client-available-qty")) + " шт."
            : boxPlanWarning(stockRow, selected);
        }
      }
      tr.setAttribute("data-move-qty", String(selected.qty || 0));
      tr.classList.toggle("is-selected", selected.qty > 0);
      tr.classList.toggle("has-box-error", exceedsAvailable);
    });
    syncMovementMixedBoxPicker();
    applyMovementStockGrid();
  }
  function renderMovementStock(data, merge) {
    if (!merge) movementStockByBarcode = {};
    (data.results || []).forEach(function (row) { movementStockByBarcode[row.barcode] = row; });
    byId("fbs-movement-lines").querySelectorAll("tr").forEach(function (tr) {
      var input = tr.querySelector('[data-field="barcode"]');
      var row = input ? movementStockByBarcode[input.value.trim()] : null;
      if (row) applyMovementStockToLine(tr, row);
    });
    byId("fbs-movement-stock-count").textContent = number(data.total) + " позиций";
    var stockKpis = [
      kpi("Доступных позиций", number(data.total), "можно добавить в заявку", ""),
      kpi("Свободно для заявки", number(data.summary.client_available_qty), "штук", "accent")
    ];
    if (movementMode() === "box") {
      stockKpis.push(kpi("Свободных целых коробов", number(data.summary.source_box_count), "монокороба", ""));
      stockKpis.push(kpi("Свободных микс-коробов", number(data.summary.source_mixed_box_count), "выбираются целиком", ""));
    }
    byId("fbs-movement-stock-kpis").innerHTML = stockKpis.join("");
    renderMovementMixedBoxes(data, merge);
    byId("fbs-movement-stock-body").innerHTML = (data.results || []).length ? data.results.map(function (row, index) {
      var itemMode = movementMode() === "item";
      var disabled = Number(row.client_available_qty || 0) <= 0 || (!itemMode && !selectableMovementBoxOptions(row).length);
      var placeMeta = "Свободно: " + number(row.client_available_qty) + " шт.";
      var goodsTypeFull = String(row.goods_type || "Готовый");
      var goodsTypeShort = goodsTypeFull
        .replace(/Разрешено размещение из приёмки/gi, "из приёмки")
        .replace(/Готово на хранении/gi, "на хранении")
        .replace(/Готовый товар/gi, "готовый");
      var productFilter = [row.sku_code, row.name, row.brand, row.size].filter(Boolean).join(" ");
      var brandMeta = row.brand ? "Бренд: " + row.brand : "Бренд не указан";
      return '<tr class="' + (disabled ? "is-unavailable" : "") + '" data-source-index="' + index +
        '" data-product="' + esc(productFilter) + '" data-brand="' + esc(row.brand || "") + '" data-sku-code="' + esc(row.sku_code) +
        '" data-barcode="' + esc(row.barcode) + '" data-client-available-qty="' + Number(row.client_available_qty || 0) +
        '" data-fbs-available-qty="' + Number(row.fbs_available_qty || 0) +
        '" data-move-qty="0"><td><strong>' +
        esc(row.sku_code || "-") + "</strong><span>" + esc(row.name || "-") + (row.size ? " · " + esc(row.size) : "") +
        "</span><small>" + esc(brandMeta) + "</small></td><td>" + esc(row.barcode) + '</td><td>' +
        '<span class="fbs-goods-type" title="' + esc(goodsTypeFull) + '">' + esc(goodsTypeShort) + '</span></td><td class="numeric"><strong>' +
        number(row.client_available_qty) + '</strong><small>можно включить в новую заявку</small></td>' +
        '<td class="numeric"><strong>' + number(row.fbs_available_qty || 0) + '</strong><small>' +
        (Number(row.fbs_available_qty || 0) > 0 ? 'уже лежит на складе FBS' : 'на FBS пока нет') +
        '</small></td><td><input type="number" class="fbs-move-qty" min="0" max="' + Number(row.client_available_qty || 0) +
        '" step="1" inputmode="numeric" data-barcode="' + esc(row.barcode) + '" placeholder="шт." aria-label="Итого к перемещению, ' + esc(row.sku_code || row.barcode) + '"' +
        (disabled ? " disabled" : "") + '></td><td data-box-line-column data-box-plan>' + availableBoxesHtml(row) +
        '</td><td class="numeric" data-box-line-column data-box-count><span class="fbs-box-plan-empty">—</span><small data-box-warning></small></td>' +
        '<td><button type="button" class="fbs-stock-reset" data-barcode="' + esc(row.barcode) +
        '" title="Убрать позицию" aria-label="Убрать позицию">×</button></td></tr>';
    }).join("") : emptyRow(9, "Свободного товара по фильтру нет.");
    syncMovementStockPicker();
  }
  function movementStockFilters() {
    return {
      article: byId("fbs-movement-stock-article").value.trim(),
      brand: byId("fbs-movement-stock-brand").value.trim(),
      barcode: byId("fbs-movement-stock-barcode").value.trim()
    };
  }
  function loadMovementStock(force) {
    flushPendingMovementQtyCommits();
    var filters = movementStockFilters();
    var hasFilters = Boolean(filters.article || filters.brand || filters.barcode);
    var cacheKey = "movementStock:" + movementMode();
    movementStockLoading = true;
    movementStockLoaded = false;
    updateMovementSubmitState();
    if (!hasFilters && cache[cacheKey] && !force) {
      renderMovementStock(cache[cacheKey], false);
      movementStockLoading = false;
      movementStockLoaded = true;
      updateMovementSubmitState();
      return Promise.resolve();
    }
    var params = new URLSearchParams(filters);
    params.set("mode", movementMode());
    byId("fbs-movement-stock-body").innerHTML = emptyRow(9, "Загрузка...");
    return api("/client/api/v1/fbs/movement-stock/?" + params.toString()).then(function (data) {
      if (!hasFilters) cache[cacheKey] = data;
      renderMovementStock(data, hasFilters);
      movementStockLoaded = true;
    }).catch(function (error) {
      movementStockLoaded = false;
      fail(error);
    }).finally(function () {
      movementStockLoading = false;
      updateMovementSubmitState();
    });
  }
  function resolveMovementBarcode(input) {
    var barcode = input.value.trim();
    var tr = input.closest("tr");
    if (!barcode) {
      applyMovementStockToLine(tr, null);
      return;
    }
    if (movementStockByBarcode[barcode]) {
      applyMovementStockToLine(tr, movementStockByBarcode[barcode]);
      return;
    }
    api("/client/api/v1/fbs/movement-stock/?barcode=" + encodeURIComponent(barcode) + "&mode=" + encodeURIComponent(movementMode())).then(function (data) {
      var exact = (data.results || []).find(function (row) { return row.barcode === barcode; });
      if (exact) movementStockByBarcode[barcode] = exact;
      applyMovementStockToLine(tr, exact || null);
    }).catch(fail);
  }
  function resolveOrAppendMovementBarcode(input) {
    var tr = input.closest("tr");
    var barcode = input.value.trim();
    var committed = tr ? String(tr.getAttribute("data-committed-barcode") || "") : "";
    if (tr && committed && barcode && barcode !== committed) {
      input.value = committed;
      if (movementStockByBarcode[committed]) applyMovementStockToLine(tr, movementStockByBarcode[committed]);
      var nextLine = findMovementLine(barcode);
      if (!nextLine) {
        addMovementRow({ barcode: barcode });
        nextLine = findMovementLine(barcode);
      }
      if (nextLine) {
        var nextInput = nextLine.querySelector('[data-field="barcode"]');
        resolveMovementBarcode(nextInput);
        nextLine.querySelector('[data-field="qty"]').focus();
      }
      updateMovementTotal();
      return;
    }
    resolveMovementBarcode(input);
  }
  function movementRows() {
    var itemMode = movementMode() === "item";
    return Array.prototype.map.call(byId("fbs-movement-lines").querySelectorAll("tr"), function (tr, index) {
      var barcode = tr.querySelector('[data-field="barcode"]').value.trim();
      var requestedQty = Number(tr.querySelector('[data-field="qty"]').value || 0);
      var plan = itemMode
        ? { totalQty: requestedQty, boxCount: 0, items: [], remainder: 0 }
        : movementBoxPlan(movementStockByBarcode[barcode], requestedQty);
      return {
        row_number: index + 1,
        barcode: barcode,
        requested_qty: requestedQty,
        qty: itemMode ? requestedQty : plan.totalQty,
        units_per_box: itemMode || plan.items.length !== 1 ? 1 : plan.items[0].units_per_box,
        box_count: itemMode ? 0 : plan.boxCount,
        box_plan: itemMode ? [] : plan.items,
        remainder: itemMode ? 0 : plan.remainder
      };
    }).filter(function (row) { return row.barcode || Number(row.requested_qty || 0) > 0; });
  }
  function updateMovementTotal(syncPicker) {
    var rows = movementRows();
    var selectedRows = movementMode() === "box" ? rows.filter(function (row) {
      return Number(row.requested_qty || 0) > 0;
    }) : rows;
    var mixedBoxes = selectedMovementMixedBoxes();
    var qty = selectedRows.reduce(function (sum, row) { return sum + (Number(row.qty) || 0); }, 0) +
      mixedBoxes.reduce(function (sum, box) { return sum + Number(box.total_qty || 0); }, 0);
    var remainderQty = selectedRows.reduce(function (sum, row) { return sum + Number(row.remainder || 0); }, 0);
    var itemMode = movementMode() === "item";
    var plan = movementRequestBoxPlan();
    var boxes = itemMode ? plan.boxCount : selectedRows.reduce(function (sum, row) {
      return sum + (Number(row.qty) > 0 ? (Number(row.box_count) || 0) : 0);
    }, 0) + mixedBoxes.length;
    var positionKeys = {};
    selectedRows.forEach(function (row) { if (row.barcode) positionKeys[row.barcode] = true; });
    mixedBoxes.forEach(function (box) {
      (box.items || []).forEach(function (item) { if (item.barcode) positionKeys[item.barcode] = true; });
    });
    var mixedCount = itemMode ? plan.mixedBoxCount : mixedBoxes.length;
    byId("fbs-movement-total").textContent = "К перемещению: " + number(qty) + " шт. · позиций: " + Object.keys(positionKeys).length + " · физических коробов: " + number(boxes) + " · микс: " + number(mixedCount) +
      (remainderQty ? " · не войдёт: " + number(remainderQty) + " шт." : "");
    renderMovementMixedBoxSelection();
    if (syncPicker !== false) syncMovementStockPicker();
    saveMovementDraft();
  }
  function syncMovementManualLineRequirements(tr) {
    if (!tr) return;
    var barcodeInput = tr.querySelector('[data-field="barcode"]');
    var qtyInput = tr.querySelector('[data-field="qty"]');
    if (!barcodeInput || !qtyInput) return;
    var hasManualValues = Boolean(barcodeInput.value.trim() || qtyInput.value.trim());
    barcodeInput.required = hasManualValues;
    qtyInput.required = hasManualValues;
  }
  function addMovementRow(row) {
    row = row || {};
    var itemMode = movementMode() === "item";
    var inputQty = itemMode ? row.qty : (row.requested_qty == null ? row.qty : row.requested_qty);
    var tr = document.createElement("tr");
    tr.innerHTML = '<td class="fbs-selected-product" data-value="product">' + movementProductHtml(row) +
      '</td><td><input data-field="barcode" type="text" inputmode="numeric" aria-label="Штрихкод товара" value="' + esc(row.barcode || "") +
      '" required></td><td data-value="goods_type">' + esc(row.goods_type || "-") +
      '</td><td><input data-field="qty" type="number" min="1" step="1" value="' + esc(inputQty || "") +
      '" required></td><td data-box-line-column><div data-box-plan-result><span class="fbs-box-plan-empty">Укажите количество</span></div>' +
      '<select data-field="units_per_box" hidden><option value="1" selected>1</option></select></td>' +
      '<td data-box-line-column class="numeric"><strong data-box-count-value>—</strong>' +
      '<input data-field="box_count" type="number" value="" hidden><small data-box-warning></small></td><td class="numeric" data-value="client_available">' +
      (row.general_available_qty == null ? "-" : number(row.general_available_qty)) +
      '</td><td><button type="button" class="row-remove" title="Удалить строку" aria-label="Удалить строку">×</button></td>';
    byId("fbs-movement-lines").appendChild(tr);
    if (row.barcode && (row.sku_code || row.name || row.product_name)) {
      tr.setAttribute("data-committed-barcode", String(row.barcode));
    }
    if (row.general_available_qty != null) {
      tr.querySelector('[data-field="qty"]').max = String(Number(row.general_available_qty || 0));
    }
    syncMovementManualLineRequirements(tr);
    applyModeToRows();
    updateMovementTotal(false);
  }
  function renderMovementRows(rows) {
    byId("fbs-movement-lines").innerHTML = "";
    (rows && rows.length ? rows : [{}]).forEach(addMovementRow);
    updateMovementTotal();
  }
  function applyModeToRows(deriveBoxMultiple) {
    var itemMode = movementMode() === "item";
    var itemPlan = byId("fbs-item-box-plan");
    itemPlan.hidden = !itemMode;
    byId("fbs-movement-request-box-count").disabled = !itemMode;
    byId("fbs-movement-mixed-box-count").disabled = !itemMode;
    var mixedPicker = byId("fbs-movement-mixed-box-picker");
    if (mixedPicker) mixedPicker.hidden = itemMode;
    var selectAllBoxes = byId("fbs-movement-select-all-boxes");
    if (selectAllBoxes) selectAllBoxes.hidden = itemMode;
    root.querySelectorAll("[data-box-line-column]").forEach(function (cell) {
      cell.hidden = itemMode;
    });
    byId("fbs-movement-lines").querySelectorAll("tr").forEach(function (tr) {
      syncMovementLineCalculation(tr, Boolean(deriveBoxMultiple));
    });
    syncMovementStockPicker();
  }
  function movementErrors(error) {
    var box = byId("fbs-movement-errors");
    var details = error && error.details && error.details.length ? error.details : [error && error.message ? error.message : "Проверьте заявку."];
    box.innerHTML = details.map(function (message) { return "<p>" + esc(message) + "</p>"; }).join("");
    box.hidden = false;
  }
  function clearMovementErrors() { byId("fbs-movement-errors").hidden = true; }
  function prepareMovementSubmission() {
    var rows = movementRows();
    var errors = [];
    if (movementMode() === "item") {
      if (!rows.length) errors.push("Добавьте хотя бы одну позицию.");
      return { rows: rows, errors: errors };
    }
    rows = rows.filter(function (row) { return Number(row.requested_qty || 0) > 0; });
    if (!rows.length && !selectedMovementMixedBoxCodes.length) {
      errors.push("Выберите хотя бы один целый или микс-короб.");
      return { rows: rows, errors: errors };
    }
    var prepared = rows.map(function (row) {
      var tr = findMovementLine(row.barcode);
      var stockRow = movementStockByBarcode[row.barcode];
      var label = stockRow ? (stockRow.sku_code || row.barcode) : row.barcode;
      if (!tr || !stockRow) {
        errors.push("Товар " + (label || "без штрихкода") + ": актуальный остаток не загружен. Обновите остатки и выберите короб повторно.");
        return row;
      }
      var requestedQty = Number(row.requested_qty || 0);
      var plan = movementBoxPlan(stockRow, requestedQty);
      if (!plan.boxCount || !plan.totalQty) {
        errors.push("Товар " + label + ": из указанного количества нельзя собрать ни одного доступного целого монокороба.");
        return row;
      }
      if (plan.totalQty > Number(stockRow.client_available_qty || 0)) {
        errors.push("Товар " + label + ": для " + number(plan.totalQty) + " шт. недостаточно актуального остатка.");
        return row;
      }
      return {
        row_number: row.row_number,
        barcode: row.barcode,
        qty: String(plan.totalQty),
        units_per_box: String(plan.items.length === 1 ? plan.items[0].units_per_box : 1),
        box_count: String(plan.boxCount),
        box_plan: plan.items
      };
    });
    updateMovementTotal();
    return { rows: prepared, errors: errors };
  }
  function importMovementFile(file) {
    if (!file) return;
    clearMovementErrors();
    var form = new FormData();
    form.append("mode", movementMode());
    form.append("file", file);
    byId("fbs-movement-file-name").textContent = "Проверка файла...";
    api("/client/api/v1/fbs/movements/import/", {
      method: "POST",
      headers: { "X-CSRFToken": csrfToken() },
      body: form
    }).then(function (data) {
      sourceFileName = data.filename || file.name || "";
      byId("fbs-movement-file-name").textContent = sourceFileName;
      if (movementMode() === "item") {
        var importedBoxCount = (data.lines || []).reduce(function (sum, line) {
          return sum + Math.max(Number(line.box_count || 1), 1);
        }, 0);
        byId("fbs-movement-request-box-count").value = String(Math.max(importedBoxCount, 1));
        byId("fbs-movement-mixed-box-count").value = "0";
      }
      renderMovementRows(data.lines || []);
    }).catch(function (error) {
      sourceFileName = "";
      byId("fbs-movement-file-name").textContent = file.name || "Файл не прошел проверку";
      movementErrors(error);
    });
  }
  function submitMovement(event) {
    event.preventDefault();
    clearMovementErrors();
    if (movementStockLoading || !movementStockLoaded) {
      movementErrors({ details: [movementStockLoading
        ? "Дождитесь обновления актуальных остатков и кратности коробов."
        : "Актуальные остатки не загружены. Нажмите «Обновить остатки» и повторите отправку."] });
      return;
    }
    flushPendingMovementQtyCommits();
    var prepared = prepareMovementSubmission();
    if (prepared.errors.length) {
      movementErrors({ details: prepared.errors });
      return;
    }
    var plan = movementRequestBoxPlan();
    movementSubmitting = true;
    updateMovementSubmitState();
    api("/client/api/v1/fbs/movements/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify({
        mode: movementMode(),
        lines: prepared.rows,
        requested_box_count: movementMode() === "item" ? plan.boxCount : null,
        requested_mixed_box_count: movementMode() === "item" ? plan.mixedBoxCount : 0,
        mixed_box_codes: movementMode() === "box" ? selectedMovementMixedBoxCodes.slice() : [],
        comment: byId("fbs-movement-comment").value || "",
        source_file_name: sourceFileName,
        idempotency_key: movementIdempotencyKey
      })
    }).then(function (data) {
      cache.movements = null;
      cache.overview = null;
      delete cache["movementStock:item"];
      delete cache["movementStock:box"];
      sourceFileName = "";
      selectedMovementMixedBoxCodes = [];
      movementMixedBoxByCode = {};
      clearMovementDraft();
      movementIdempotencyKey = newMovementIdempotencyKey();
      byId("fbs-movement-form").reset();
      byId("fbs-movement-request-box-count").value = "1";
      byId("fbs-movement-mixed-box-count").value = "0";
      renderMovementRows([{}]);
      location.hash = "#/fbs?tab=movements&movement=" + encodeURIComponent(data.id);
      if (window.showWip) window.showWip("Заявка FBS создана");
    }).catch(movementErrors).finally(function () {
      movementSubmitting = false;
      updateMovementSubmitState();
    });
  }

  function ensure() {
    var info = hashInfo();
    if (info.path !== "/fbs") return;
    showAlert("");
    var tab = activatePanel(requestedTab(info));
    if (tab !== "overview" && !cache.overview) loadOverview(false);
    if (info.params.get("group") && tab === "orders") byId("fbs-order-group").value = info.params.get("group");
    if (tab === "overview") loadOverview(false);
    else if (tab === "orders") loadOrders();
    else if (tab === "order-detail") loadOrderDetail(info.params.get("order"));
    else if (tab === "stock") loadStock(false);
    else if (tab === "stock-detail") loadStockDetail(info.params.get("barcode"));
    else if (tab === "reports") loadReports(false);
    else if (tab === "outbound" || tab === "outbound-new") loadOutbound(false);
    else if (tab === "movements") loadMovements(false);
    else if (tab === "movement-detail") loadMovementDetail(info.params.get("movement"));
    else if (tab === "movement-new") {
      if (!byId("fbs-movement-lines").children.length && !restoreMovementDraft()) renderMovementRows([{}]);
      applyModeToRows();
      loadMovementStock(false);
    }
  }

  byId("fbs-refresh").addEventListener("click", function () {
    cache = {};
    ozonWarehouseSettingsLoaded = false;
    var info = hashInfo();
    var tab = requestedTab(info);
    if (tab === "overview") loadOverview(true);
    else if (tab === "orders") loadOrders();
    else if (tab === "stock") loadStock(true);
    else if (tab === "reports") loadReports(true);
    else if (tab === "outbound" || tab === "outbound-new") { cache.outbound = null; loadOutbound(true); }
    else if (tab === "movements") loadMovements(true);
    else ensure();
  });
  byId("fbs-ozon-warehouses-open").addEventListener("click", function () { loadOzonWarehouseSettings(false); });
  byId("fbs-ozon-warehouses-close").addEventListener("click", function () { byId("fbs-ozon-warehouses-settings").hidden = true; });
  byId("fbs-ozon-warehouses-save").addEventListener("click", saveOzonWarehouseSettings);
  byId("fbs-order-apply").addEventListener("click", function () { orderPage = 1; loadOrders(); });
  byId("fbs-order-search").addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); orderPage = 1; loadOrders(); } });
  byId("fbs-orders-prev").addEventListener("click", function () { if (orderPage > 1) { orderPage -= 1; loadOrders(); } });
  byId("fbs-orders-next").addEventListener("click", function () { if (orderPage < orderPages) { orderPage += 1; loadOrders(); } });
  byId("fbs-stock-apply").addEventListener("click", function () { loadStock(true); });
  byId("fbs-stock-search").addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); loadStock(true); } });
  byId("fbs-outbound-search-button").addEventListener("click", function () { cache.outbound = null; loadOutbound(true); });
  byId("fbs-outbound-search-clear").addEventListener("click", function () {
    byId("fbs-outbound-search").value = "";
    cache.outbound = null;
    loadOutbound(true);
  });
  byId("fbs-outbound-search").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { event.preventDefault(); cache.outbound = null; loadOutbound(true); }
  });
  byId("fbs-outbound-stock-body").addEventListener("input", function (event) {
    if (event.target.matches(".fbs-outbound-qty")) updateOutboundSelection();
  });
  byId("fbs-outbound-delivery-type").addEventListener("change", updateOutboundDeliveryFields);
  byId("fbs-outbound-file-button").addEventListener("click", function () { byId("fbs-outbound-file").click(); });
  byId("fbs-outbound-file").addEventListener("change", function () { importOutboundFile(this.files && this.files[0]); });
  byId("fbs-outbound-form").addEventListener("submit", submitOutbound);
  byId("fbs-movement-stock-apply").addEventListener("click", function () { loadMovementStock(true); });
  byId("fbs-movement-stock-clear").addEventListener("click", function () {
    ["fbs-movement-stock-article", "fbs-movement-stock-brand", "fbs-movement-stock-barcode"].forEach(function (id) {
      byId(id).value = "";
    });
    loadMovementStock(false);
  });
  byId("fbs-movement-stock-refresh").addEventListener("click", function () {
    delete cache["movementStock:item"];
    delete cache["movementStock:box"];
    loadMovementStock(true);
  });
  byId("fbs-movement-select-all-boxes").addEventListener("click", function () {
    if (movementMode() !== "box") return;
    var rows = Object.keys(movementStockByBarcode).map(function (barcode) {
      var row = movementStockByBarcode[barcode];
      var plan = movementAllBoxPlan(row);
      if (!plan.totalQty) return null;
      return {
        barcode: row.barcode,
        sku_code: row.sku_code,
        product_name: row.name || row.product_name,
        brand: row.brand,
        goods_type: row.goods_type || "Готовый",
        requested_qty: plan.totalQty,
        qty: plan.totalQty,
        general_available_qty: row.client_available_qty,
        fbs_available_qty: row.fbs_available_qty
      };
    }).filter(Boolean);
    selectedMovementMixedBoxCodes = Object.keys(movementMixedBoxByCode);
    renderMovementRows(rows.length ? rows : [{}]);
    syncMovementMixedBoxPicker();
    updateMovementTotal(false);
  });
  ["fbs-movement-stock-article", "fbs-movement-stock-brand", "fbs-movement-stock-barcode"].forEach(function (id) {
    byId(id).addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); loadMovementStock(true); }
    });
  });
  byId("fbs-movement-stock-body").addEventListener("input", function (event) {
    if (!event.target.matches(".fbs-move-qty")) return;
    scheduleMovementStockQuantity(event.target);
  });
  byId("fbs-movement-stock-body").addEventListener("focusout", function (event) {
    if (!event.target.matches(".fbs-move-qty")) return;
    commitMovementStockQuantity(event.target);
  });
  byId("fbs-movement-stock-body").addEventListener("keydown", function (event) {
    if (!event.target.matches(".fbs-move-qty") || event.key !== "Enter") return;
    event.preventDefault();
    commitMovementStockQuantity(event.target);
  });
  byId("fbs-movement-stock-body").addEventListener("click", function (event) {
    var button = event.target.closest(".fbs-stock-reset");
    if (!button) return;
    removeMovementStockSelection(button.getAttribute("data-barcode"));
  });
  byId("fbs-movement-mixed-box-grid").addEventListener("change", function (event) {
    if (!event.target.matches("[data-mixed-box-control]")) return;
    var card = event.target.closest("[data-mixed-box-code]");
    var code = card ? card.getAttribute("data-mixed-box-code") : "";
    if (!code) return;
    setMovementMixedBoxSelected(code, event.target.checked);
    syncMovementMixedBoxPicker();
    updateMovementTotal(false);
  });
  byId("fbs-movement-mixed-selection-cards").addEventListener("click", function (event) {
    var button = event.target.closest("[data-remove-mixed-box]");
    if (!button) return;
    setMovementMixedBoxSelected(button.getAttribute("data-remove-mixed-box"), false);
    syncMovementMixedBoxPicker();
    updateMovementTotal(false);
  });
  root.querySelectorAll(".fbs-stock-filter").forEach(function (input) {
    input.addEventListener("input", applyMovementStockGrid);
  });
  root.querySelectorAll(".fbs-stock-sort").forEach(function (button) {
    button.addEventListener("click", function () {
      var key = button.getAttribute("data-sort-key") || "";
      if (movementStockSort.key === key) movementStockSort.dir = movementStockSort.dir === "asc" ? "desc" : "asc";
      else movementStockSort = { key: key, dir: "asc", type: button.getAttribute("data-sort-type") || "text" };
      root.querySelectorAll(".fbs-stock-sort").forEach(function (item) {
        var indicator = item.querySelector("span");
        var active = item === button;
        item.classList.toggle("active", active);
        if (indicator) indicator.textContent = active ? (movementStockSort.dir === "asc" ? "↑" : "↓") : "⇅";
      });
      applyMovementStockGrid();
    });
  });
  byId("fbs-add-movement-line").addEventListener("click", function () { addMovementRow({}); });
  byId("fbs-movement-lines").addEventListener("click", function (event) {
    var button = event.target.closest(".row-remove");
    if (!button) return;
    button.closest("tr").remove();
    if (!byId("fbs-movement-lines").children.length) addMovementRow({});
    updateMovementTotal();
  });
  byId("fbs-movement-lines").addEventListener("input", function (event) {
    if (event.target.matches('[data-field="barcode"]')) {
      var tr = event.target.closest("tr");
      var committed = String(tr.getAttribute("data-committed-barcode") || "");
      var barcode = event.target.value.trim();
      if (!committed || barcode === committed) {
        var exact = movementStockByBarcode[barcode];
        applyMovementStockToLine(tr, exact || null);
      }
    }
    var movementLine = event.target.closest("tr");
    syncMovementManualLineRequirements(movementLine);
    syncMovementLineCalculation(movementLine, false);
    updateMovementTotal();
  });
  byId("fbs-movement-lines").addEventListener("change", function (event) {
    if (event.target.matches('[data-field="barcode"]')) resolveOrAppendMovementBarcode(event.target);
    updateMovementTotal();
  });
  byId("fbs-movement-lines").addEventListener("keydown", function (event) {
    if (!event.target.matches('[data-field="barcode"]') || event.key !== "Enter") return;
    event.preventDefault();
    resolveOrAppendMovementBarcode(event.target);
  });
  root.querySelectorAll('input[name="fbs_mode"]').forEach(function (radio) {
    radio.addEventListener("change", function () {
      clearPendingMovementQtyCommits();
      movementStockLoaded = false;
      movementStockByBarcode = {};
      movementMixedBoxByCode = {};
      selectedMovementMixedBoxCodes = [];
      updateMovementSubmitState();
      applyModeToRows(true);
      sourceFileName = "";
      byId("fbs-movement-file").value = "";
      byId("fbs-movement-file-name").textContent = "Файл не выбран";
      clearMovementErrors();
      updateMovementTotal();
      loadMovementStock(true);
    });
  });
  ["fbs-movement-request-box-count", "fbs-movement-mixed-box-count"].forEach(function (id) {
    byId(id).addEventListener("input", function () {
      var plan = movementRequestBoxPlan();
      byId("fbs-movement-mixed-box-count").max = String(Math.max(plan.boxCount, 0));
      updateMovementTotal();
    });
  });
  byId("fbs-movement-comment").addEventListener("input", saveMovementDraft);
  byId("fbs-movement-file-button").addEventListener("click", function () { byId("fbs-movement-file").click(); });
  byId("fbs-movement-file").addEventListener("change", function () { importMovementFile(this.files && this.files[0]); });
  byId("fbs-movement-form").addEventListener("submit", submitMovement);
  var templateLink = root.querySelector('a[href="/client/fbs/movement-template.xlsx"]');
  if (templateLink) templateLink.href = clientUrl("/client/fbs/movement-template.xlsx");

  window.__lkEnsureFbs = ensure;
  // The LK router calls __lkEnsureFbs on hashchange. A second listener used to
  // load each FBS page twice. The initial call below handles script startup.
  updateMovementSubmitState();
  ensure();
})();
