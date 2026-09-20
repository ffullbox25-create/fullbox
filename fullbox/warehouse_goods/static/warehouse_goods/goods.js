(function () {
  "use strict";

  var shell = document.getElementById("dashboard-shell");
  var collapseButton = document.getElementById("sidebar-collapse");
  var sidebarStorageKey = "head-manager-sidebar-collapsed";
  if (shell && collapseButton) {
    function renderSidebar(collapsed) {
      shell.classList.toggle("nav-collapsed", collapsed);
      collapseButton.setAttribute("aria-expanded", collapsed ? "false" : "true");
      var label = collapseButton.querySelector(".btn-label");
      var icon = collapseButton.querySelector(".nav-icon");
      if (label) label.textContent = collapsed ? "Развернуть меню" : "Свернуть меню";
      if (icon) icon.textContent = collapsed ? "›" : "‹";
    }
    var sidebarCollapsed = window.localStorage.getItem(sidebarStorageKey) === "1";
    renderSidebar(sidebarCollapsed);
    collapseButton.addEventListener("click", function () {
      sidebarCollapsed = !sidebarCollapsed;
      window.localStorage.setItem(sidebarStorageKey, sidebarCollapsed ? "1" : "0");
      renderSidebar(sidebarCollapsed);
    });
  }

  document.querySelectorAll("[data-row-link]").forEach(function (row) {
    function openRow(event) {
      if (event.target.closest("a,button,input,select,textarea")) return;
      window.location.href = row.dataset.rowLink;
    }
    row.addEventListener("dblclick", openRow);
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter") openRow(event);
    });
  });

  var selectAll = document.querySelector("[data-select-all]");
  var rowSelections = Array.from(document.querySelectorAll("[data-row-select]"));
  var selectionActions = Array.from(document.querySelectorAll("[data-requires-selection]"));
  function refreshSelection() {
    var count = rowSelections.filter(function (checkbox) { return checkbox.checked; }).length;
    rowSelections.forEach(function (checkbox) {
      var row = checkbox.closest("tr");
      if (row) row.classList.toggle("selected", checkbox.checked);
    });
    if (selectAll) {
      selectAll.checked = rowSelections.length > 0 && count === rowSelections.length;
      selectAll.indeterminate = count > 0 && count < rowSelections.length;
    }
    selectionActions.forEach(function (button) { button.disabled = count === 0; });
  }
  if (selectAll) {
    selectAll.addEventListener("change", function () {
      rowSelections.forEach(function (checkbox) { checkbox.checked = selectAll.checked; });
      refreshSelection();
    });
  }
  rowSelections.forEach(function (checkbox) { checkbox.addEventListener("change", refreshSelection); });
  refreshSelection();

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-modal-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      var modal = document.getElementById(button.dataset.modalOpen);
      if (modal && modal.showModal) modal.showModal();
    });
  });
  document.querySelectorAll("[data-modal-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      var modal = button.closest("dialog");
      if (modal) modal.close();
    });
  });
  document.querySelectorAll("dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  });

  function bindFilterSortTables(root) {
    (root || document).querySelectorAll("[data-filter-sort-table]").forEach(function (table) {
      if (table.dataset.filterSortBound === "1") return;
      table.dataset.filterSortBound = "1";
      var body = table.tBodies[0];
      if (!body) return;
      var rows = Array.from(body.querySelectorAll("tr[data-filter-sort-row]"));
      var filters = Array.from(table.querySelectorAll("[data-column-filter]"));
      var sortButtons = Array.from(table.querySelectorAll("[data-sort-column]"));
      if (!rows.length) {
        filters.forEach(function (input) { input.disabled = true; });
        sortButtons.forEach(function (button) { button.disabled = true; });
        return;
      }
      var originalOrder = new Map(rows.map(function (row, index) { return [row, index]; }));
      var filterEmptyRow = document.createElement("tr");
      filterEmptyRow.className = "table-filter-empty";
      filterEmptyRow.innerHTML = '<td colspan="' + table.tHead.rows[0].cells.length + '">По заданным фильтрам ничего не найдено</td>';
      var activeSortColumn = null;
      var activeSortDirection = "asc";

      function cellText(row, column) {
        var cell = row.cells[column];
        return cell ? String(cell.textContent || "").trim() : "";
      }

      function applyFilters() {
        var visibleCount = 0;
        rows.forEach(function (row) {
          var visible = filters.every(function (input) {
            var needle = String(input.value || "").trim().toLocaleLowerCase("ru-RU");
            return !needle || cellText(row, Number(input.dataset.columnFilter)).toLocaleLowerCase("ru-RU").includes(needle);
          });
          row.hidden = !visible;
          if (visible) visibleCount += 1;
        });
        filterEmptyRow.remove();
        if (!visibleCount) body.appendChild(filterEmptyRow);
      }

      filters.forEach(function (input) { input.addEventListener("input", applyFilters); });
      sortButtons.forEach(function (button) {
        button.addEventListener("click", function () {
          var column = Number(button.dataset.sortColumn);
          var type = button.dataset.sortType || "text";
          activeSortDirection = activeSortColumn === column && activeSortDirection === "asc" ? "desc" : "asc";
          activeSortColumn = column;
          sortButtons.forEach(function (item) {
            var active = Number(item.dataset.sortColumn) === column;
            item.classList.toggle("is-active", active);
            item.closest("th")?.setAttribute("aria-sort", active ? (activeSortDirection === "asc" ? "ascending" : "descending") : "none");
            var icon = item.querySelector("span");
            if (icon) icon.textContent = active ? (activeSortDirection === "asc" ? "▲" : "▼") : "⇅";
          });
          rows.sort(function (left, right) {
            var leftValue = cellText(left, column);
            var rightValue = cellText(right, column);
            var comparison;
            if (type === "number") {
              comparison = (Number(leftValue.replace(/\s/g, "").replace(",", ".")) || 0)
                - (Number(rightValue.replace(/\s/g, "").replace(",", ".")) || 0);
            } else {
              comparison = leftValue.localeCompare(rightValue, "ru", { numeric: true, sensitivity: "base" });
            }
            if (!comparison) comparison = originalOrder.get(left) - originalOrder.get(right);
            return activeSortDirection === "asc" ? comparison : -comparison;
          });
          filterEmptyRow.remove();
          rows.forEach(function (row) { body.appendChild(row); });
          applyFilters();
        });
      });
    });
  }

  bindFilterSortTables(document);

  var tabs = document.querySelector("[data-tabs]");
  var panel = document.getElementById("tab-panel");
  if (tabs && panel) {
    var cache = {};
    var initial = tabs.dataset.initialTab || "about";
    var valid = Array.from(tabs.querySelectorAll("[data-tab]")).map(function (button) { return button.dataset.tab; });
    if (valid.indexOf(initial) === -1) initial = "about";

    function activate(tab, pushState) {
      tabs.querySelectorAll("[data-tab]").forEach(function (button) {
        button.classList.toggle("active", button.dataset.tab === tab);
      });
      if (cache[tab]) {
        panel.innerHTML = cache[tab];
        bindFragment();
      } else {
        panel.innerHTML = '<div class="loading-state">Загрузка данных…</div>';
        fetch(panel.dataset.tabUrl.replace("__tab__", tab), {headers: {"X-Requested-With": "XMLHttpRequest"}})
          .then(function (response) { if (!response.ok) throw new Error("Ошибка " + response.status); return response.text(); })
          .then(function (html) { cache[tab] = html; panel.innerHTML = html; bindFragment(); })
          .catch(function (error) { panel.innerHTML = '<div class="empty-state"><strong>Не удалось загрузить вкладку</strong><span>' + error.message + '</span></div>'; });
      }
      if (pushState) {
        var url = new URL(window.location.href);
        url.searchParams.set("tab", tab);
        history.replaceState({}, "", url.toString());
      }
    }
    tabs.addEventListener("click", function (event) {
      var button = event.target.closest("[data-tab]");
      if (button) activate(button.dataset.tab, true);
    });
    function bindFragment() {
      bindFilterSortTables(panel);
      panel.querySelectorAll("form[data-confirm]").forEach(function (form) {
        form.addEventListener("submit", function (event) {
          if (!window.confirm(form.dataset.confirm)) event.preventDefault();
        });
      });
    }
    activate(initial, false);
  }

  var labelForm = document.getElementById("label-form");
  if (labelForm) {
    var templateSelect = document.getElementById("label-template");
    var printedCode = labelForm.querySelector("#label-preview small");
    var barcodeValue = printedCode ? printedCode.textContent.trim() : "0000000000000";
    function renderBarcode() {
      if (!window.JsBarcode) return;
      try { window.JsBarcode("#label-barcode", barcodeValue, {format: "CODE128", height: 62, width: 2, displayValue: false, margin: 0}); }
      catch (error) { window.JsBarcode("#label-barcode", "0000000000000", {format: "CODE128", height: 62, displayValue: false}); }
    }
    renderBarcode();

    function csrf() { return labelForm.querySelector("[name=csrfmiddlewaretoken]").value; }
    function selectedSize() {
      var option = templateSelect.options[templateSelect.selectedIndex];
      return {width: option.dataset.width, height: option.dataset.height};
    }
    function capture() {
      if (!window.html2canvas) return Promise.reject(new Error("Модуль предпросмотра недоступен"));
      return window.html2canvas(document.getElementById("label-preview"), {scale: 3, backgroundColor: "#ffffff"})
        .then(function (canvas) { return canvas.toDataURL("image/png"); });
    }
    function payload(image) {
      var size = selectedSize();
      var data = new FormData();
      data.append("csrfmiddlewaretoken", csrf());
      data.append("image", image);
      data.append("template_key", templateSelect.value);
      data.append("width_mm", size.width);
      data.append("height_mm", size.height);
      data.append("copies", labelForm.querySelector("[name=copies]").value || "1");
      return data;
    }
    function status(text, isError) {
      var target = document.getElementById("label-status");
      target.textContent = text || "";
      target.style.color = isError ? "#b83232" : "#15805c";
    }
    labelForm.querySelector("[data-label-pdf]").addEventListener("click", function () {
      status("Формируем PDF…");
      capture().then(function (image) {
        return fetch(labelForm.dataset.pdfUrl, {method: "POST", body: payload(image)});
      }).then(function (response) {
        if (!response.ok) return response.json().then(function (body) { throw new Error(body.error || "Ошибка PDF"); });
        return response.blob();
      }).then(function (blob) {
        var link = document.createElement("a");
        link.href = URL.createObjectURL(blob); link.download = "goods-labels.pdf"; link.click();
        setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        status("PDF готов.");
      }).catch(function (error) { status(error.message, true); });
    });
    labelForm.querySelector("[data-label-queue]").addEventListener("click", function () {
      status("Отправляем в очередь печати…");
      capture().then(function (image) {
        return fetch(labelForm.dataset.queueUrl, {method: "POST", body: payload(image)});
      }).then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error(body.error); return body; }); })
        .then(function (body) { status(body.message || "Отправлено в печать."); })
        .catch(function (error) { status(error.message, true); });
    });
  }
})();
