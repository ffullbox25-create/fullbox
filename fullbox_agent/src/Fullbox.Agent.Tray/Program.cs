using System.Diagnostics;
using System.Drawing;
using System.Drawing.Text;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Fullbox.Agent.Service;
using Fullbox.Agent.Shared;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Tray;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();

        ConfigStore.LoadOrCreate();

        Icon? trayIcon = null;
        Icon? okIcon = null;
        Icon? errorIcon = null;
        IntPtr okHandle = IntPtr.Zero;
        IntPtr errorHandle = IntPtr.Zero;
        try
        {
            var asm = Assembly.GetExecutingAssembly();
            using var stream = asm.GetManifestResourceStream("Fullbox.Agent.Tray.Assets.FullboxTray.ico");
            if (stream != null)
            {
                trayIcon = new Icon(stream);
            }
        }
        catch
        {
            trayIcon = null;
        }

        try
        {
            okIcon = CreateStatusIcon(Color.FromArgb(34, 139, 34), "F", out okHandle);
            errorIcon = CreateStatusIcon(Color.FromArgb(176, 45, 45), "F", out errorHandle);
        }
        catch
        {
            okIcon = null;
            errorIcon = null;
        }

        var notify = new NotifyIcon
        {
            Icon = trayIcon ?? SystemIcons.Application,
            Text = "Fullbox Agent",
            Visible = true,
        };

        var host = BuildHost();
        StartHost(host, notify);

        var runtime = host.Services.GetRequiredService<AgentRuntime>();
        var scanner = host.Services.GetRequiredService<ComScanner>();
        var printerController = host.Services.GetRequiredService<PrinterController>();
        var printStatusStore = host.Services.GetRequiredService<PrintStatusStore>();
        var bridgeLogger = host.Services.GetRequiredService<ILogger<LocalAgentBridgeServer>>();
        using var bridgeServer = new LocalAgentBridgeServer(
            () => BuildSnapshot(runtime, scanner, printerController, printStatusStore),
            bridgeLogger
        );

        try
        {
            bridgeServer.Start();
        }
        catch (Exception ex)
        {
            bridgeLogger.LogWarning(ex, "Failed to start local agent bridge");
        }

        var statusTimer = new System.Windows.Forms.Timer { Interval = 1000 };
        HardwareState lastState = new(false, "init", "");
        DiagnosticsForm? diagnosticsForm = null;

        var menu = new ContextMenuStrip();
        menu.Items.Add("Принтеры...", null, (_, _) => OpenDiagnostics(DiagnosticsTab.Printers));
        menu.Items.Add("Сканеры...", null, (_, _) => OpenDiagnostics(DiagnosticsTab.Scanners));
        menu.Items.Add("Общее состояние...", null, (_, _) => OpenDiagnostics(DiagnosticsTab.Overview));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Настройки агента", null, (_, _) => OpenConfig());
        menu.Items.Add("Папка агента", null, (_, _) => OpenConfigFolder());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Выход", null, (_, _) => ExitApp());
        notify.ContextMenuStrip = menu;

        notify.DoubleClick += (_, _) => OpenDiagnostics(DiagnosticsTab.Overview);

        statusTimer.Tick += (_, _) =>
        {
            var snapshot = BuildSnapshot(runtime, scanner, printerController, printStatusStore);
            if (snapshot.Hardware.Ready != lastState.Ready || snapshot.Hardware.Reason != lastState.Reason)
            {
                lastState = snapshot.Hardware;
                var nextIcon = snapshot.Hardware.Ready ? okIcon : errorIcon;
                notify.Icon = nextIcon ?? trayIcon ?? SystemIcons.Application;
                notify.Text = snapshot.Hardware.Ready
                    ? $"Fullbox Agent ({snapshot.Hardware.Reason})"
                    : $"Fullbox Agent ({snapshot.Hardware.Reason})";
            }

            if (diagnosticsForm != null && !diagnosticsForm.IsDisposed)
            {
                diagnosticsForm.UpdateSnapshot(snapshot);
            }
        };
        statusTimer.Start();

        Application.ApplicationExit += (_, _) =>
            StopHost(host, bridgeServer, statusTimer, notify, ref okHandle, ref errorHandle);
        Application.Run();

        void OpenConfig()
        {
            var path = ConfigStore.ConfigPath;
            var psi = new ProcessStartInfo("notepad.exe", $"\"{path}\"")
            {
                UseShellExecute = true,
            };
            Process.Start(psi);
        }

        void OpenConfigFolder()
        {
            var path = ConfigStore.ConfigDir;
            var psi = new ProcessStartInfo("explorer.exe", $"\"{path}\"")
            {
                UseShellExecute = true,
            };
            Process.Start(psi);
        }

        void OpenWindowsPrinters()
        {
            var psi = new ProcessStartInfo("explorer.exe", "shell:PrintersFolder")
            {
                UseShellExecute = true,
            };
            Process.Start(psi);
        }

        void ExitApp()
        {
            StopHost(host, bridgeServer, statusTimer, notify, ref okHandle, ref errorHandle);
            Application.Exit();
        }

        void OpenDiagnostics(DiagnosticsTab tab)
        {
            if (diagnosticsForm == null || diagnosticsForm.IsDisposed)
            {
                diagnosticsForm = new DiagnosticsForm();
                diagnosticsForm.RefreshRequested += () =>
                {
                    var snap = BuildSnapshot(runtime, scanner, printerController, printStatusStore);
                    diagnosticsForm.UpdateSnapshot(snap);
                };
                diagnosticsForm.ApplyRequested += change => ApplyConfig(change, runtime, scanner);
                diagnosticsForm.OpenPrintersRequested += OpenWindowsPrinters;
            }

            diagnosticsForm.SelectTab(tab);
            diagnosticsForm.UpdateSnapshot(BuildSnapshot(runtime, scanner, printerController, printStatusStore));
            diagnosticsForm.Show();
            diagnosticsForm.BringToFront();
            diagnosticsForm.Activate();
        }
    }

    private static IHost BuildHost()
    {
        return Host.CreateDefaultBuilder()
            .ConfigureServices(services =>
            {
                services.AddSingleton<AgentRuntime>();
                services.AddSingleton<ComScanner>();
                services.AddSingleton<PrinterController>();
                services.AddSingleton<PrintAgentClient>();
                services.AddSingleton<PrintStatusStore>();
                services.AddSingleton<PrintJobRunner>();
                services.AddHostedService<Worker>();
            })
            .Build();
    }

    private static void StartHost(IHost hostInstance, NotifyIcon notify)
    {
        try
        {
            hostInstance.StartAsync().GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            notify.ShowBalloonTip(4000, "Fullbox Agent", $"Ошибка запуска агента: {ex.Message}", ToolTipIcon.Error);
        }
    }

    private static void StopHost(
        IHost hostInstance,
        LocalAgentBridgeServer bridgeServer,
        System.Windows.Forms.Timer statusTimer,
        NotifyIcon notify,
        ref IntPtr okHandle,
        ref IntPtr errorHandle
    )
    {
        try
        {
            statusTimer.Stop();
            bridgeServer.Stop();
            hostInstance.StopAsync(TimeSpan.FromSeconds(4)).GetAwaiter().GetResult();
        }
        catch
        {
            // ignore shutdown errors
        }
        finally
        {
            notify.Visible = false;
            notify.Dispose();
            if (okHandle != IntPtr.Zero)
            {
                DestroyIcon(okHandle);
                okHandle = IntPtr.Zero;
            }
            if (errorHandle != IntPtr.Zero)
            {
                DestroyIcon(errorHandle);
                errorHandle = IntPtr.Zero;
            }
            hostInstance.Dispose();
        }
    }

    private static DiagnosticsSnapshot BuildSnapshot(
        AgentRuntime runtimeInstance,
        ComScanner comScanner,
        PrinterController printerController,
        PrintStatusStore printStatusStore
    )
    {
        var comStatus = comScanner.GetStatus();
        var ports = comScanner.ListPorts();
        var devices = runtimeInstance.GetComDevicesSnapshot();
        var state = ComputeHardwareState(runtimeInstance, comStatus, ports);
        var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0.0.0";
        return new DiagnosticsSnapshot(
            DateTime.Now,
            state,
            runtimeInstance.Config,
            version,
            comStatus,
            ports,
            devices,
            printerController.GetDefaultPrinterName(),
            printerController.ListPrinterDetails(),
            printStatusStore.Snapshot()
        );
    }

    private static HardwareState ComputeHardwareState(
        AgentRuntime runtimeInstance,
        Dictionary<string, object> comStatus,
        IReadOnlyList<string> ports
    )
    {
        var mode = (runtimeInstance.Config.ScannerMode ?? "Com").Trim();
        if (!string.Equals(mode, "Com", StringComparison.OrdinalIgnoreCase))
        {
            return new HardwareState(false, $"Режим {mode} не поддержан этой сборкой", "mode_not_supported");
        }

        var config = runtimeInstance.Config.Com;
        var enabled = config.Enabled;
        var port = (config.PortName ?? "").Trim();
        var connected = comStatus.TryGetValue("connected", out var connectedObj) && connectedObj is bool boolean && boolean;
        var error = comStatus.TryGetValue("error", out var errorObj) ? errorObj?.ToString() ?? "" : "";
        var portPresent = ports.Any(item => item.Equals(port, StringComparison.OrdinalIgnoreCase));

        if (!enabled)
        {
            return new HardwareState(false, "Сканер отключен", "disabled");
        }

        if (string.IsNullOrWhiteSpace(port))
        {
            return new HardwareState(false, "Порт не задан", "port_missing");
        }

        if (!portPresent)
        {
            return new HardwareState(false, "Порт не найден", "port_not_found");
        }

        if (connected)
        {
            return new HardwareState(true, "Подключено", "connected");
        }

        if (!string.IsNullOrWhiteSpace(error))
        {
            return new HardwareState(false, $"Ошибка: {error}", "error");
        }

        return new HardwareState(false, "Не подключено", "not_connected");
    }

    private static void ApplyConfig(DiagnosticsConfigChange change, AgentRuntime runtimeInstance, ComScanner comScanner)
    {
        var config = runtimeInstance.Config;
        config.ScannerMode = string.IsNullOrWhiteSpace(change.ScannerMode) ? "Com" : change.ScannerMode.Trim();
        config.Com.Enabled = change.Enabled;
        config.Com.PortName = change.Port;
        config.Com.BaudRate = change.Baud;
        config.Com.Eol = change.Eol;
        config.Com.IdleMs = change.IdleMs;
        config.Keyboard.MinLength = change.KeyboardMinLength;
        config.Keyboard.MaxInterKeyMs = change.KeyboardMaxInterKeyMs;
        config.Keyboard.Suffix = change.KeyboardSuffix;
        ConfigStore.Save(config);
        runtimeInstance.ReloadConfig();

        if (!change.Enabled || !string.Equals(config.ScannerMode, "Com", StringComparison.OrdinalIgnoreCase))
        {
            comScanner.Disable();
        }
        else
        {
            comScanner.RequestReconnect();
        }
    }

    private static Icon CreateStatusIcon(Color background, string text, out IntPtr handle)
    {
        var bmp = new Bitmap(16, 16);
        using var g = Graphics.FromImage(bmp);
        g.Clear(background);
        g.TextRenderingHint = TextRenderingHint.SingleBitPerPixelGridFit;
        using var font = new Font("Segoe UI", 9, FontStyle.Bold, GraphicsUnit.Pixel);
        var size = g.MeasureString(text, font);
        var x = (16 - size.Width) / 2f;
        var y = (16 - size.Height) / 2f - 1;
        using var brush = new SolidBrush(Color.White);
        g.DrawString(text, font, brush, x, y);
        handle = bmp.GetHicon();
        return Icon.FromHandle(handle);
    }

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern bool DestroyIcon(IntPtr handle);
}
