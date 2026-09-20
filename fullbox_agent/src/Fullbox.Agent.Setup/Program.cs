using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Principal;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Threading;

namespace Fullbox.Agent.Setup;

internal static class Program
{
    private const string AgentName = "FullboxAgent";
    private const string BundleResourceName = "Fullbox.Agent.Bundle";
    private const string TrayExeName = "Fullbox.Agent.Tray.exe";

    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0.0.0";
        var form = new InstallerForm(version);
        form.Shown += async (_, _) => await RunInstallAsync(form);
        Application.Run(form);
    }

    private static async Task RunInstallAsync(InstallerForm form)
    {
        form.SetTotalSteps(10);

        try
        {
            var stepAdmin = form.BeginStep("Проверка прав администратора", Environment.UserName);
            if (!IsAdministrator())
            {
                form.FailStep(stepAdmin, "Нужны права администратора.");
                form.AllowClose("Подтвердите запрос UAC для продолжения.");
                RelaunchAsAdmin();
                return;
            }
            form.CompleteStep(stepAdmin, "Права администратора подтверждены.");

            var tempZip = Path.Combine(Path.GetTempPath(), $"fullbox_agent_bundle_{Guid.NewGuid():N}.zip");
            var tempDir = Path.Combine(Path.GetTempPath(), $"fullbox_agent_bundle_{Guid.NewGuid():N}");
            Directory.CreateDirectory(tempDir);

            if (!await RunStepAsync(form, "Подготовка встроенного пакета", "Встроенный пакет",
                    async () =>
                    {
                        await ExtractEmbeddedBundleAsync(tempZip);
                        return $"Пакет сохранен: {tempZip}";
                    }))
            {
                return;
            }

            if (!await RunStepAsync(form, "Распаковка пакета", tempDir,
                    () => Task.Run(() =>
                    {
                        ZipFile.ExtractToDirectory(tempZip, tempDir, true);
                        return "Распаковка завершена.";
                    })))
            {
                return;
            }

            var agentDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "FullboxAgent");
            var binDir = Path.Combine(agentDir, "bin");
            if (!await RunStepAsync(form, "Подготовка папок установки", binDir,
                    () => Task.Run(() =>
                    {
                        Directory.CreateDirectory(binDir);
                        return $"Готово: {binDir}";
                    })))
            {
                return;
            }

            if (!await RunStepAsync(form, "Остановка сервиса и трея", "Fullbox Agent",
                    () => Task.Run(() => StopRunningProcesses())))
            {
                return;
            }

            var trayDst = Path.Combine(binDir, TrayExeName);

            if (!await RunStepAsync(form, "Отключение сервиса", AgentName,
                    () => Task.Run(() => DisableServiceIfPresent())))
            {
                return;
            }

            if (!await RunStepAsync(form, "Копирование агента", trayDst,
                    () => Task.Run(() =>
                    {
                        return CopyRuntimeFiles(tempDir, binDir);
                    })))
            {
                return;
            }

            var configPath = Path.Combine(agentDir, "config.json");
            if (!await RunStepAsync(form, "Обновление конфигурации", configPath,
                    () => Task.Run(() =>
                    {
                        MergeConfig(Path.Combine(tempDir, "config.json"), configPath);
                        return "Конфигурация обновлена.";
                    })))
            {
                return;
            }

            if (!await RunStepAsync(form, "Создание автозапуска агента", trayDst,
                    () => Task.Run(() =>
                    {
                        CreateStartupShortcut(trayDst);
                        return "Автозапуск создан.";
                    })))
            {
                return;
            }

            if (!await RunStepAsync(form, "Запуск агента", trayDst,
                    () => Task.Run(() => StartTray(trayDst))))
            {
                return;
            }

            form.MarkSuccess();
        }
        catch (Exception ex)
        {
            form.MarkFailure(ex.Message);
        }
    }

    private static async Task<bool> RunStepAsync(InstallerForm form, string title, string details, Func<Task<string>> action)
    {
        var step = form.BeginStep(title, details);
        try
        {
            var detail = await action();
            form.CompleteStep(step, detail);
            return true;
        }
        catch (Exception ex)
        {
            form.FailStep(step, ex.Message);
            form.MarkFailure(ex.Message);
            return false;
        }
    }

    private static bool IsAdministrator()
    {
        using var identity = WindowsIdentity.GetCurrent();
        var principal = new WindowsPrincipal(identity);
        return principal.IsInRole(WindowsBuiltInRole.Administrator);
    }

    private static void RelaunchAsAdmin()
    {
        var exe = Process.GetCurrentProcess().MainModule?.FileName;
        if (string.IsNullOrWhiteSpace(exe))
        {
            MessageBox.Show("Не удалось перезапустить установщик.", "Fullbox Agent", MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return;
        }

        var psi = new ProcessStartInfo(exe)
        {
            UseShellExecute = true,
            Verb = "runas",
        };
        try
        {
            Process.Start(psi);
        }
        catch
        {
            MessageBox.Show("Нужны права администратора.", "Fullbox Agent", MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
        }
    }

    private static async Task ExtractEmbeddedBundleAsync(string targetPath)
    {
        var assembly = Assembly.GetExecutingAssembly();
        await using var stream = assembly.GetManifestResourceStream(BundleResourceName);
        if (stream == null)
        {
            throw new InvalidOperationException("Встроенный пакет не найден.");
        }

        await using var output = File.Create(targetPath);
        await stream.CopyToAsync(output);
    }

    private static void MergeConfig(string sourcePath, string targetPath)
    {
        var target = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
        if (File.Exists(targetPath))
        {
            try
            {
                var existing = JsonSerializer.Deserialize<Dictionary<string, object?>>(File.ReadAllText(targetPath));
                if (existing != null)
                {
                    foreach (var pair in existing)
                    {
                        target[pair.Key] = pair.Value;
                    }
                }
            }
            catch
            {
                target = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
            }
        }

        if (File.Exists(sourcePath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(sourcePath));
                var root = doc.RootElement;
                CopyStringIfPresent(root, target, "baseUrl");
                CopyStringIfPresent(root, target, "token");
                CopyStringIfPresent(root, target, "printToken");
                CopyStringIfPresent(root, target, "printAgentName");
                CopyBoolIfPresent(root, target, "realtimeEnabled");
                CopyNumberIfPresent(root, target, "commandFallbackPollIntervalSec");
                CopyNumberIfPresent(root, target, "printFallbackPollIntervalSec");
                CopyNumberIfPresent(root, target, "printPollIntervalSec");
                CopyNumberIfPresent(root, target, "printJobTimeoutSec");
            }
            catch
            {
                // ignore invalid source config
            }
        }

        EnsureDefaultFields(target);
        var json = JsonSerializer.Serialize(target, new JsonSerializerOptions { WriteIndented = true });
        Directory.CreateDirectory(Path.GetDirectoryName(targetPath) ?? string.Empty);
        File.WriteAllText(targetPath, json);
    }

    private static void EnsureDefaultFields(Dictionary<string, object?> target)
    {
        if (!target.TryGetValue("agentId", out var agentId) || string.IsNullOrWhiteSpace(Convert.ToString(agentId)))
        {
            target["agentId"] = $"pc-{Guid.NewGuid():N}".Substring(0, 11);
        }
        if (!target.TryGetValue("name", out var name) || string.IsNullOrWhiteSpace(Convert.ToString(name)))
        {
            target["name"] = Environment.MachineName;
        }
        if (!target.TryGetValue("host", out var host) || string.IsNullOrWhiteSpace(Convert.ToString(host)))
        {
            target["host"] = Environment.MachineName;
        }
        if (!target.TryGetValue("baseUrl", out var baseUrl) || string.IsNullOrWhiteSpace(Convert.ToString(baseUrl)))
        {
            target["baseUrl"] = "https://fullbox.ru";
        }
        if (
            !target.TryGetValue("realtimeEnabled", out var realtimeEnabled)
            || !bool.TryParse(Convert.ToString(realtimeEnabled), out var enabled)
            || !enabled
        )
        {
            target["realtimeEnabled"] = true;
        }
    }

    private static void CopyStringIfPresent(JsonElement root, Dictionary<string, object?> target, string key)
    {
        if (!root.TryGetProperty(key, out var element))
        {
            return;
        }
        if (element.ValueKind == JsonValueKind.String)
        {
            var value = element.GetString();
            if (!string.IsNullOrWhiteSpace(value))
            {
                target[key] = value;
            }
        }
    }

    private static void CopyBoolIfPresent(JsonElement root, Dictionary<string, object?> target, string key)
    {
        if (!root.TryGetProperty(key, out var element))
        {
            return;
        }
        if (element.ValueKind == JsonValueKind.True || element.ValueKind == JsonValueKind.False)
        {
            target[key] = element.GetBoolean();
            return;
        }
        if (element.ValueKind == JsonValueKind.String && bool.TryParse(element.GetString(), out var value))
        {
            target[key] = value;
        }
    }

    private static void CopyNumberIfPresent(JsonElement root, Dictionary<string, object?> target, string key)
    {
        if (!root.TryGetProperty(key, out var element))
        {
            return;
        }
        if (element.ValueKind == JsonValueKind.Number && element.TryGetInt32(out var number))
        {
            target[key] = number;
            return;
        }
        if (element.ValueKind == JsonValueKind.String && int.TryParse(element.GetString(), out var value))
        {
            target[key] = value;
        }
    }

    private static (int ExitCode, string Output) RunSc(string args)
    {
        var psi = new ProcessStartInfo("sc.exe", args)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        using var proc = Process.Start(psi);
        if (proc == null)
        {
            return (-1, "Failed to start sc.exe");
        }
        var output = proc.StandardOutput.ReadToEnd();
        var error = proc.StandardError.ReadToEnd();
        proc.WaitForExit();
        var combined = string.Join(Environment.NewLine, new[] { output, error }).Trim();
        return (proc.ExitCode, combined);
    }

    private static void EnsureScSuccess((int ExitCode, string Output) result, string stepName)
    {
        if (result.ExitCode != 0)
        {
            throw new InvalidOperationException($"{stepName} failed: {result.Output}");
        }
    }
    private static string DisableServiceIfPresent()
    {
        var status = RunSc($"query {AgentName}");
        if (status.ExitCode != 0)
        {
            return "Сервис не найден.";
        }

        var stopResult = RunSc($"stop {AgentName}");
        if (stopResult.ExitCode != 0 && stopResult.ExitCode != 1062)
        {
            throw new InvalidOperationException($"Service stop failed: {stopResult.Output}");
        }
        WaitForServiceStop(TimeSpan.FromSeconds(10));
        EnsureScSuccess(RunSc($"config {AgentName} start= disabled"), "service disable");
        return "Сервис отключен.";
    }

    private static string StopRunningProcesses()
    {
        var stoppedService = false;
        var serviceResult = RunSc($"query {AgentName}");
        if (serviceResult.ExitCode == 0)
        {
            var stopResult = RunSc($"stop {AgentName}");
            if (stopResult.ExitCode == 0 || stopResult.ExitCode == 1062)
            {
                stoppedService = true;
            }
            WaitForServiceStop(TimeSpan.FromSeconds(10));
        }

        var stoppedTray = StopProcessByName("Fullbox.Agent.Tray");
        return $"Сервис: {(stoppedService ? "остановлен" : "не запущен")}, агент: {(stoppedTray ? "закрыт" : "не найден")}.";
    }

    private static void WaitForServiceStop(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            var status = RunSc($"query {AgentName}");
            if (status.ExitCode != 0)
            {
                return;
            }
            if (status.Output.IndexOf("STOPPED", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return;
            }
            Thread.Sleep(500);
        }
    }


    private static bool StopProcessByName(string name)
    {
        var stopped = false;
        try
        {
            foreach (var proc in Process.GetProcessesByName(name))
            {
                try
                {
                    proc.Kill(true);
                    proc.WaitForExit(3000);
                    stopped = true;
                }
                catch
                {
                    // ignore
                }
            }
        }
        catch
        {
            // ignore
        }
        return stopped;
    }

    private static void CopyWithRetry(string source, string destination)
    {
        const int attempts = 6;
        for (var i = 0; i < attempts; i++)
        {
            try
            {
                File.Copy(source, destination, true);
                return;
            }
            catch (IOException ex)
            {
                if (i == attempts - 1)
                {
                    throw new IOException(TranslateIoError(ex.Message), ex);
                }
                Thread.Sleep(800);
            }
        }
    }

    private static string TranslateIoError(string message)
    {
        if (message.Contains("being used by another process", StringComparison.OrdinalIgnoreCase))
        {
            return "Файл занят другим процессом.";
        }
        return message;
    }

    private static string CopyRuntimeFiles(string sourceDir, string destinationDir)
    {
        Directory.CreateDirectory(destinationDir);
        var copied = 0;
        foreach (var source in Directory.EnumerateFiles(sourceDir))
        {
            var name = Path.GetFileName(source);
            var extension = Path.GetExtension(source);
            var isRuntimeFile =
                string.Equals(name, TrayExeName, StringComparison.OrdinalIgnoreCase)
                || string.Equals(extension, ".dll", StringComparison.OrdinalIgnoreCase)
                || name.EndsWith(".runtimeconfig.json", StringComparison.OrdinalIgnoreCase);
            if (!isRuntimeFile)
            {
                continue;
            }

            CopyWithRetry(source, Path.Combine(destinationDir, name));
            copied++;
        }

        if (!File.Exists(Path.Combine(destinationDir, TrayExeName)))
        {
            throw new InvalidOperationException("Tray executable was not found in the embedded package.");
        }

        return $"Runtime files copied: {copied}.";
    }

    private static void CreateStartupShortcut(string trayExePath)
    {
        var startupDir = Environment.GetFolderPath(Environment.SpecialFolder.Startup);
        var linkPath = Path.Combine(startupDir, "FullboxAgentTray.lnk");
        var vbsPath = Path.Combine(startupDir, "FullboxAgentTray.vbs");
        if (File.Exists(vbsPath))
        {
            try
            {
                File.Delete(vbsPath);
            }
            catch
            {
                // ignore
            }
        }

        var shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType == null)
        {
            throw new InvalidOperationException("WScript.Shell is not available.");
        }
        dynamic shell = Activator.CreateInstance(shellType) ?? throw new InvalidOperationException("Failed to create WScript.Shell.");
        dynamic shortcut = shell.CreateShortcut(linkPath);
        shortcut.TargetPath = trayExePath;
        shortcut.WindowStyle = 7;
        shortcut.Save();
    }

    private static string StartTray(string trayExePath)
    {
        var psi = new ProcessStartInfo(trayExePath)
        {
            UseShellExecute = true,
            WorkingDirectory = Path.GetDirectoryName(trayExePath) ?? string.Empty,
        };
        Process.Start(psi);
        Thread.Sleep(2500);
        if (!IsTrayRunning())
        {
            var logPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "FullboxAgent",
                "logs",
                "tray.crash.log"
            );
            throw new InvalidOperationException($"Agent exited immediately. Log: {logPath}");
        }
        return "Agent started and is still running.";
    }

    private static bool IsTrayRunning()
    {
        try
        {
            var processes = Process.GetProcessesByName("Fullbox.Agent.Tray");
            foreach (var proc in processes)
            {
                proc.Dispose();
            }
            return processes.Length > 0;
        }
        catch
        {
            return true;
        }
    }
}
