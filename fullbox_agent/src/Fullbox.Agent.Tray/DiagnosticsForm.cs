using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Windows.Forms;
using Fullbox.Agent.Service;
using Fullbox.Agent.Shared;

namespace Fullbox.Agent.Tray;

public sealed class DiagnosticsForm : Form
{
    private readonly Label _headline;
    private readonly Label _processLine;
    private readonly Button _refreshButton;
    private readonly Button _copyButton;
    private readonly TabControl _tabs;
    private readonly TextBox _overviewText;
    private readonly CheckBox _scannerEnabled;
    private readonly ComboBox _modeSelect;
    private readonly ComboBox _portSelect;
    private readonly ComboBox _baudSelect;
    private readonly ComboBox _eolSelect;
    private readonly NumericUpDown _idleSelect;
    private readonly NumericUpDown _hidMinLengthSelect;
    private readonly NumericUpDown _hidMaxPauseSelect;
    private readonly ComboBox _hidSuffixSelect;
    private readonly Button _applyScannerButton;
    private readonly Label _scannerState;
    private readonly TextBox _scannerDetails;
    private readonly Label _printerState;
    private readonly Button _openPrintersButton;
    private readonly TextBox _printerDetails;
    private readonly List<string> _scanHistory = new();
    private bool _dirty;
    private string _lastScanToken = "";

    public DiagnosticsForm()
    {
        Text = "Fullbox Agent";
        Width = 1180;
        Height = 820;
        MinimumSize = new System.Drawing.Size(1040, 720);
        StartPosition = FormStartPosition.CenterScreen;

        _headline = new Label
        {
            Dock = DockStyle.Top,
            Height = 34,
            Font = new System.Drawing.Font("Segoe UI", 12, System.Drawing.FontStyle.Bold),
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
        };

        _processLine = new Label
        {
            Dock = DockStyle.Top,
            Height = 28,
            Font = new System.Drawing.Font("Segoe UI", 9),
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
        };

        _refreshButton = new Button
        {
            Text = "Обновить",
            Width = 110,
            Height = 32,
            Margin = new Padding(0, 0, 8, 0),
        };
        _refreshButton.Click += (_, _) => RefreshRequested?.Invoke();

        _copyButton = new Button
        {
            Text = "Копировать",
            Width = 110,
            Height = 32,
            Margin = new Padding(0),
        };
        _copyButton.Click += (_, _) => CopyCurrentText();

        var buttonsPanel = new FlowLayoutPanel
        {
            Dock = DockStyle.Right,
            Width = 240,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = false,
            Padding = new Padding(0, 16, 0, 0),
        };
        buttonsPanel.Controls.Add(_refreshButton);
        buttonsPanel.Controls.Add(_copyButton);

        var headerTextPanel = new Panel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(0, 4, 0, 0),
        };
        headerTextPanel.Controls.Add(_processLine);
        headerTextPanel.Controls.Add(_headline);

        var headerPanel = new Panel
        {
            Dock = DockStyle.Top,
            Height = 92,
            Padding = new Padding(12, 12, 12, 0),
        };
        headerPanel.Controls.Add(headerTextPanel);
        headerPanel.Controls.Add(buttonsPanel);

        _overviewText = CreateTextArea();

        _scannerEnabled = new CheckBox
        {
            Text = "Сканер включен",
            AutoSize = true,
            Dock = DockStyle.Fill,
        };
        _scannerEnabled.CheckedChanged += (_, _) => MarkDirty();

        _modeSelect = CreateComboBox("Com", "Keyboard");
        _modeSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _portSelect = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDownList,
        };
        _portSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _baudSelect = CreateComboBox("9600", "19200", "38400", "57600", "115200");
        _baudSelect.TextChanged += (_, _) => MarkDirty();

        _eolSelect = CreateComboBox(
            ComEol.CrLf.ToString(),
            ComEol.Cr.ToString(),
            ComEol.Lf.ToString(),
            ComEol.Tab.ToString(),
            ComEol.None.ToString()
        );
        _eolSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _idleSelect = CreateNumeric(0, 10000, 1);
        _idleSelect.ValueChanged += (_, _) => MarkDirty();

        _hidMinLengthSelect = CreateNumeric(0, 1024, 1);
        _hidMinLengthSelect.ValueChanged += (_, _) => MarkDirty();

        _hidMaxPauseSelect = CreateNumeric(0, 1000, 1);
        _hidMaxPauseSelect.ValueChanged += (_, _) => MarkDirty();

        _hidSuffixSelect = CreateComboBox("EnterOrTab", "Enter", "Tab", "None");
        _hidSuffixSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _applyScannerButton = new Button
        {
            Text = "Применить настройки",
            Dock = DockStyle.Right,
            Width = 180,
            Height = 32,
            Enabled = false,
        };
        _applyScannerButton.Click += (_, _) => ApplyConfig();

        var scannerSettings = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            ColumnCount = 4,
            RowCount = 5,
            Padding = new Padding(12, 10, 12, 0),
        };
        scannerSettings.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 160));
        scannerSettings.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        scannerSettings.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 160));
        scannerSettings.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        for (var index = 0; index < 5; index++)
        {
            scannerSettings.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
        }

        scannerSettings.Controls.Add(CreateFieldLabel("Состояние:"), 0, 0);
        scannerSettings.Controls.Add(_scannerEnabled, 1, 0);
        scannerSettings.Controls.Add(CreateFieldLabel("Режим:"), 2, 0);
        scannerSettings.Controls.Add(_modeSelect, 3, 0);

        scannerSettings.Controls.Add(CreateFieldLabel("Порт:"), 0, 1);
        scannerSettings.Controls.Add(_portSelect, 1, 1);
        scannerSettings.Controls.Add(CreateFieldLabel("Скорость:"), 2, 1);
        scannerSettings.Controls.Add(_baudSelect, 3, 1);

        scannerSettings.Controls.Add(CreateFieldLabel("Окончание строки:"), 0, 2);
        scannerSettings.Controls.Add(_eolSelect, 1, 2);
        scannerSettings.Controls.Add(CreateFieldLabel("Таймаут, мс:"), 2, 2);
        scannerSettings.Controls.Add(_idleSelect, 3, 2);

        scannerSettings.Controls.Add(CreateFieldLabel("Мин. длина HID:"), 0, 3);
        scannerSettings.Controls.Add(_hidMinLengthSelect, 1, 3);
        scannerSettings.Controls.Add(CreateFieldLabel("Макс. пауза HID, мс:"), 2, 3);
        scannerSettings.Controls.Add(_hidMaxPauseSelect, 3, 3);

        scannerSettings.Controls.Add(CreateFieldLabel("Суффикс HID:"), 0, 4);
        scannerSettings.Controls.Add(_hidSuffixSelect, 1, 4);
        scannerSettings.Controls.Add(new Panel(), 2, 4);
        scannerSettings.Controls.Add(_applyScannerButton, 3, 4);

        _scannerState = new Label
        {
            Dock = DockStyle.Top,
            Height = 62,
            Padding = new Padding(16, 10, 16, 0),
            Font = new System.Drawing.Font("Segoe UI", 9),
        };

        _scannerDetails = CreateTextArea();

        var scannerPage = new TabPage("Сканеры");
        scannerPage.Controls.Add(_scannerDetails);
        scannerPage.Controls.Add(_scannerState);
        scannerPage.Controls.Add(scannerSettings);

        _printerState = new Label
        {
            Dock = DockStyle.Top,
            Height = 34,
            Padding = new Padding(16, 8, 16, 0),
            Font = new System.Drawing.Font("Segoe UI", 9),
        };

        _openPrintersButton = new Button
        {
            Text = "Открыть принтеры Windows",
            Dock = DockStyle.Top,
            Height = 32,
            Margin = new Padding(12, 0, 12, 0),
        };
        _openPrintersButton.Click += (_, _) => OpenPrintersRequested?.Invoke();

        _printerDetails = CreateTextArea();

        var printerPage = new TabPage("Принтеры");
        printerPage.Controls.Add(_printerDetails);
        printerPage.Controls.Add(_openPrintersButton);
        printerPage.Controls.Add(_printerState);

        var overviewPage = new TabPage("Общие");
        overviewPage.Controls.Add(_overviewText);

        _tabs = new TabControl
        {
            Dock = DockStyle.Fill,
        };
        _tabs.TabPages.Add(overviewPage);
        _tabs.TabPages.Add(scannerPage);
        _tabs.TabPages.Add(printerPage);

        Controls.Add(_tabs);
        Controls.Add(headerPanel);
    }

    public event Action? RefreshRequested;
    public event Action<DiagnosticsConfigChange>? ApplyRequested;
    public event Action? OpenPrintersRequested;

    public void SelectTab(DiagnosticsTab tab)
    {
        _tabs.SelectedIndex = tab switch
        {
            DiagnosticsTab.Overview => 0,
            DiagnosticsTab.Scanners => 1,
            DiagnosticsTab.Printers => 2,
            _ => 0,
        };
    }

    public void UpdateSnapshot(DiagnosticsSnapshot snapshot)
    {
        _headline.Text = snapshot.Hardware.Ready
            ? $"Готов к работе: {snapshot.Hardware.Reason}"
            : $"Требует внимания: {snapshot.Hardware.Reason}";
        _headline.ForeColor = snapshot.Hardware.Ready ? System.Drawing.Color.DarkGreen : System.Drawing.Color.Maroon;
        _processLine.Text =
            $"Tray: {DescribeProcess("Fullbox.Agent.Tray")}  |  Сервис: {DescribeProcess("Fullbox.Agent.Service")}";

        UpdateInputs(snapshot);
        UpdateScanHistory(snapshot);

        _overviewText.Text = BuildOverviewText(snapshot);
        _scannerState.Text = BuildScannerStateText(snapshot);
        _scannerDetails.Text = BuildScannerDetailsText(snapshot);
        _printerState.Text = BuildPrinterStateText(snapshot.PrintStatus);
        _printerDetails.Text = BuildPrinterDetailsText(snapshot);
    }

    private void UpdateInputs(DiagnosticsSnapshot snapshot)
    {
        if (!_dirty)
        {
            _scannerEnabled.Checked = snapshot.Config.Com.Enabled;
            _modeSelect.Text = string.IsNullOrWhiteSpace(snapshot.Config.ScannerMode) ? "Com" : snapshot.Config.ScannerMode;
            _baudSelect.Text = snapshot.Config.Com.BaudRate.ToString(CultureInfo.InvariantCulture);
            _idleSelect.Value = Clamp(_idleSelect, snapshot.Config.Com.IdleMs);
            _hidMinLengthSelect.Value = Clamp(_hidMinLengthSelect, snapshot.Config.Keyboard.MinLength);
            _hidMaxPauseSelect.Value = Clamp(_hidMaxPauseSelect, snapshot.Config.Keyboard.MaxInterKeyMs);
            _hidSuffixSelect.Text = string.IsNullOrWhiteSpace(snapshot.Config.Keyboard.Suffix)
                ? "EnterOrTab"
                : snapshot.Config.Keyboard.Suffix;

            var eol = snapshot.Config.Com.Eol.ToString();
            _eolSelect.Text = string.IsNullOrWhiteSpace(eol) ? ComEol.CrLf.ToString() : eol;
        }

        var current = snapshot.Config.Com.PortName ?? "";
        var ports = snapshot.ComPorts?.ToList() ?? new List<string>();
        if (!string.IsNullOrWhiteSpace(current) && !ports.Contains(current, StringComparer.OrdinalIgnoreCase))
        {
            ports.Insert(0, current);
        }
        if (ports.Count == 0)
        {
            ports.Add("-");
        }

        if (!_portSelect.Focused && !_portSelect.DroppedDown)
        {
            _portSelect.BeginUpdate();
            _portSelect.Items.Clear();
            _portSelect.Items.AddRange(ports.Cast<object>().ToArray());
            _portSelect.SelectedItem =
                ports.FirstOrDefault(item => item.Equals(current, StringComparison.OrdinalIgnoreCase)) ?? ports[0];
            _portSelect.EndUpdate();
        }

        _applyScannerButton.Enabled = _dirty;
    }

    private void UpdateScanHistory(DiagnosticsSnapshot snapshot)
    {
        var lastAt = ReadValue(snapshot.ComStatus, "last_scan_at");
        var lastValue = ReadValue(snapshot.ComStatus, "last_scan_value");
        if (string.IsNullOrWhiteSpace(lastAt) || string.IsNullOrWhiteSpace(lastValue))
        {
            return;
        }

        var token = $"{lastAt}|{lastValue}";
        if (string.Equals(token, _lastScanToken, StringComparison.Ordinal))
        {
            return;
        }

        _lastScanToken = token;
        _scanHistory.Add($"{FormatTimestamp(lastAt)} · {lastValue}");
        while (_scanHistory.Count > 200)
        {
            _scanHistory.RemoveAt(0);
        }
    }

    private void ApplyConfig()
    {
        var port = _portSelect.SelectedItem?.ToString() ?? "";
        if (port == "-")
        {
            port = "";
        }

        if (!int.TryParse(_baudSelect.Text.Trim(), out var baud))
        {
            baud = 9600;
        }

        var eolText = string.IsNullOrWhiteSpace(_eolSelect.Text) ? ComEol.CrLf.ToString() : _eolSelect.Text.Trim();
        if (!Enum.TryParse<ComEol>(eolText, true, out var eol))
        {
            eol = ComEol.CrLf;
        }

        ApplyRequested?.Invoke(new DiagnosticsConfigChange(
            _scannerEnabled.Checked,
            string.IsNullOrWhiteSpace(_modeSelect.Text) ? "Com" : _modeSelect.Text.Trim(),
            port,
            baud,
            eol,
            (int)_idleSelect.Value,
            (int)_hidMinLengthSelect.Value,
            (int)_hidMaxPauseSelect.Value,
            string.IsNullOrWhiteSpace(_hidSuffixSelect.Text) ? "EnterOrTab" : _hidSuffixSelect.Text.Trim()
        ));

        _dirty = false;
        _applyScannerButton.Enabled = false;
    }

    private void CopyCurrentText()
    {
        var text = _tabs.SelectedIndex switch
        {
            0 => _overviewText.Text,
            1 => _scannerDetails.Text,
            2 => _printerDetails.Text,
            _ => _overviewText.Text,
        };

        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        try
        {
            Clipboard.SetText(text);
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                this,
                $"Не удалось скопировать: {ex.Message}",
                "Fullbox Agent",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }

    private void MarkDirty()
    {
        _dirty = true;
        _applyScannerButton.Enabled = true;
    }

    private string BuildOverviewText(DiagnosticsSnapshot snapshot)
    {
        var sb = new StringBuilder();
        sb.AppendLine($"Время: {snapshot.Now:dd.MM.yyyy HH:mm:ss}");
        sb.AppendLine($"Версия агента: {snapshot.Version}");
        sb.AppendLine($"Имя ПК: {Environment.MachineName}");
        sb.AppendLine($"Имя агента: {snapshot.Config.Name}");
        sb.AppendLine($"ID агента: {snapshot.Config.AgentId}");
        sb.AppendLine($"Сайт: {snapshot.Config.BaseUrl}");
        sb.AppendLine($"Режим сканера: {snapshot.Config.ScannerMode}");
        sb.AppendLine();
        sb.AppendLine("Что сейчас видно на этом компьютере:");
        sb.AppendLine($"- принтеров: {snapshot.Printers.Count}");
        sb.AppendLine($"- COM-портов: {snapshot.ComPorts.Count}");
        sb.AppendLine($"- устройств сканирования по WMI: {snapshot.ComDevices.Count}");
        sb.AppendLine();
        sb.AppendLine("Печать:");
        sb.AppendLine($"  message: {snapshot.PrintStatus.Message}");
        sb.AppendLine($"  ready: {snapshot.PrintStatus.Ready}");
        sb.AppendLine($"  state: {snapshot.PrintStatus.State}");
        sb.AppendLine($"  updated_at: {snapshot.PrintStatus.UpdatedAt:O}");
        if (!string.IsNullOrWhiteSpace(snapshot.PrintStatus.Printer))
        {
            sb.AppendLine($"  printer: {snapshot.PrintStatus.Printer}");
        }
        if (snapshot.PrintStatus.JobId.HasValue)
        {
            sb.AppendLine($"  job_id: {snapshot.PrintStatus.JobId.Value}");
        }
        if (!string.IsNullOrWhiteSpace(snapshot.PrintStatus.Error))
        {
            sb.AppendLine($"  error: {snapshot.PrintStatus.Error}");
        }
        return sb.ToString();
    }

    private string BuildScannerStateText(DiagnosticsSnapshot snapshot)
    {
        var mode = string.IsNullOrWhiteSpace(snapshot.Config.ScannerMode) ? "Com" : snapshot.Config.ScannerMode;
        if (!string.Equals(mode, "Com", StringComparison.OrdinalIgnoreCase))
        {
            return $"Выбран режим {mode}. Эта сборка сейчас восстанавливает COM-контур.";
        }

        var currentPort = snapshot.Config.Com.PortName ?? "";
        var error = ReadValue(snapshot.ComStatus, "error");
        var connected = ReadBool(snapshot.ComStatus, "connected");
        var portPresent = snapshot.ComPorts.Any(item => item.Equals(currentPort, StringComparison.OrdinalIgnoreCase));

        if (!snapshot.Config.Com.Enabled)
        {
            return "Сканер отключен.";
        }
        if (connected)
        {
            return $"Сканер подключен.\r\nВыбранный порт {currentPort} сейчас доступен на этом ПК.";
        }
        if (!portPresent)
        {
            return $"Сканер сейчас не подключен.\r\nВыбранный порт {currentPort} сейчас не найден на этом ПК.";
        }
        if (!string.IsNullOrWhiteSpace(error))
        {
            return $"Сканер сейчас не подключен.\r\nПоследняя ошибка: {error}";
        }
        return "Сканер не подключён.";
    }

    private string BuildScannerDetailsText(DiagnosticsSnapshot snapshot)
    {
        var sb = new StringBuilder();
        sb.AppendLine("Сканеры и COM-порты на этом компьютере");
        sb.AppendLine();
        sb.AppendLine("Найденные COM-порты:");
        if (snapshot.ComPorts.Count == 0)
        {
            sb.AppendLine("- не найдено");
        }
        else
        {
            foreach (var port in snapshot.ComPorts)
            {
                sb.AppendLine($"- {port}");
            }
        }
        sb.AppendLine();
        sb.AppendLine("Устройства сканирования / COM из Windows:");
        if (snapshot.ComDevices.Count == 0)
        {
            sb.AppendLine("- Windows не вернул дополнительные сведения");
        }
        else
        {
            foreach (var device in snapshot.ComDevices)
            {
                var line = $"{ReadValue(device, "port")} · {ReadValue(device, "name")}".Trim().Trim('·').Trim();
                if (string.IsNullOrWhiteSpace(line))
                {
                    line = ReadValue(device, "device_id");
                }
                sb.AppendLine($"- {line}");
                var details = new[]
                {
                    Pair("status", ReadValue(device, "status")),
                    Pair("description", ReadValue(device, "description")),
                    Pair("manufacturer", ReadValue(device, "manufacturer")),
                    Pair("service", ReadValue(device, "service")),
                }.Where(item => !string.IsNullOrWhiteSpace(item)).ToArray();
                foreach (var item in details)
                {
                    sb.AppendLine($"  {item}");
                }
            }
        }
        sb.AppendLine();
        sb.AppendLine("Последние считанные значения:");
        if (_scanHistory.Count == 0)
        {
            sb.AppendLine("- пока нет данных");
        }
        else
        {
            foreach (var item in _scanHistory.TakeLast(20))
            {
                sb.AppendLine($"- {item}");
            }
        }
        sb.AppendLine();
        sb.AppendLine("Текущий статус COM:");
        foreach (var item in snapshot.ComStatus.OrderBy(kvp => kvp.Key, StringComparer.OrdinalIgnoreCase))
        {
            sb.AppendLine($"- {item.Key}: {item.Value}");
        }

        var lastError = ReadValue(snapshot.ComStatus, "error");
        if (!string.IsNullOrWhiteSpace(lastError))
        {
            sb.AppendLine();
            sb.AppendLine($"Последняя ошибка: {lastError}");
        }

        return sb.ToString();
    }

    private string BuildPrinterStateText(PrintRuntimeStatus status)
    {
        var state = string.IsNullOrWhiteSpace(status.State) ? "unknown" : status.State;
        var message = string.IsNullOrWhiteSpace(status.Message) ? "Нет данных" : status.Message;
        return $"Состояние печати: {state} · {message}";
    }

    private string BuildPrinterDetailsText(DiagnosticsSnapshot snapshot)
    {
        var sb = new StringBuilder();
        sb.AppendLine("Что агент сообщает по печати");
        sb.AppendLine();
        sb.AppendLine($"- состояние: {snapshot.PrintStatus.State}");
        sb.AppendLine($"- сообщение: {snapshot.PrintStatus.Message}");
        sb.AppendLine($"- ready: {snapshot.PrintStatus.Ready}");
        sb.AppendLine($"- обновлено: {snapshot.PrintStatus.UpdatedAt:O}");
        if (!string.IsNullOrWhiteSpace(snapshot.PrintStatus.Printer))
        {
            sb.AppendLine($"- принтер: {snapshot.PrintStatus.Printer}");
        }
        if (snapshot.PrintStatus.JobId.HasValue)
        {
            sb.AppendLine($"- job_id: {snapshot.PrintStatus.JobId.Value}");
        }
        if (!string.IsNullOrWhiteSpace(snapshot.PrintStatus.Error))
        {
            sb.AppendLine($"- ошибка: {snapshot.PrintStatus.Error}");
        }
        sb.AppendLine();
        sb.AppendLine("Принтеры на этом компьютере");
        sb.AppendLine();
        if (snapshot.Printers.Count == 0)
        {
            sb.AppendLine("- принтеры не найдены");
        }
        else
        {
            foreach (var printer in snapshot.Printers)
            {
                sb.AppendLine($"- {printer.Name} [{DescribePrinter(printer)}]");
            }
        }
        return sb.ToString();
    }

    private static string DescribePrinter(PrinterDetailsSnapshot printer)
    {
        var flags = new List<string>
        {
            printer.IsDefault ? "по умолчанию" : "не по умолчанию",
            printer.IsLocal ? "локальный" : "сетевой",
            printer.IsOffline ? "offline" : "доступен",
        };

        if (printer.IsPaused)
        {
            flags.Add("пауза");
        }
        if (printer.IsBusy)
        {
            flags.Add("занят");
        }
        if (printer.Jobs > 0)
        {
            flags.Add($"заданий: {printer.Jobs}");
        }
        if (!string.IsNullOrWhiteSpace(printer.Status))
        {
            flags.Add(printer.Status);
        }

        return string.Join(", ", flags);
    }

    private static ComboBox CreateComboBox(params string[] items)
    {
        var combo = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDownList,
        };
        combo.Items.AddRange(items.Cast<object>().ToArray());
        if (items.Length > 0)
        {
            combo.SelectedIndex = 0;
        }
        return combo;
    }

    private static NumericUpDown CreateNumeric(decimal min, decimal max, decimal increment)
    {
        return new NumericUpDown
        {
            Dock = DockStyle.Fill,
            Minimum = min,
            Maximum = max,
            Increment = increment,
        };
    }

    private static Label CreateFieldLabel(string text)
    {
        return new Label
        {
            Text = text,
            Dock = DockStyle.Fill,
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
        };
    }

    private static TextBox CreateTextArea()
    {
        return new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical,
            Font = new System.Drawing.Font("Consolas", 10),
        };
    }

    private static string DescribeProcess(string processName)
    {
        try
        {
            var processes = Process.GetProcessesByName(processName);
            if (processes.Length == 0)
            {
                return "не запущен";
            }

            var pids = string.Join(", ", processes.Select(item => item.Id).OrderBy(item => item));
            return $"запущен (PID {pids})";
        }
        catch (Exception ex)
        {
            return $"ошибка проверки: {ex.Message}";
        }
    }

    private static string ReadValue(Dictionary<string, object> data, string key)
    {
        return data.TryGetValue(key, out var value) ? value?.ToString() ?? "" : "";
    }

    private static bool ReadBool(Dictionary<string, object> data, string key)
    {
        if (!data.TryGetValue(key, out var value) || value == null)
        {
            return false;
        }
        return value is bool boolean && boolean;
    }

    private static string FormatTimestamp(string value)
    {
        if (DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto))
        {
            return dto.ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss");
        }
        return value;
    }

    private static decimal Clamp(NumericUpDown input, int value)
    {
        var number = Convert.ToDecimal(value);
        if (number < input.Minimum)
        {
            return input.Minimum;
        }
        if (number > input.Maximum)
        {
            return input.Maximum;
        }
        return number;
    }

    private static string Pair(string key, string value)
    {
        return string.IsNullOrWhiteSpace(value) ? "" : $"{key}: {value}";
    }
}

public enum DiagnosticsTab
{
    Overview,
    Scanners,
    Printers,
}

public sealed record DiagnosticsSnapshot(
    DateTime Now,
    HardwareState Hardware,
    AgentConfig Config,
    string Version,
    Dictionary<string, object> ComStatus,
    IReadOnlyList<string> ComPorts,
    IReadOnlyList<Dictionary<string, object>> ComDevices,
    string DefaultPrinter,
    IReadOnlyList<PrinterDetailsSnapshot> Printers,
    PrintRuntimeStatus PrintStatus
);

public sealed record HardwareState(bool Ready, string Reason, string Details);

public sealed record DiagnosticsConfigChange(
    bool Enabled,
    string ScannerMode,
    string Port,
    int Baud,
    ComEol Eol,
    int IdleMs,
    int KeyboardMinLength,
    int KeyboardMaxInterKeyMs,
    string KeyboardSuffix
);
