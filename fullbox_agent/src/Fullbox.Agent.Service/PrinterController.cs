using System.Printing;
using System.Drawing.Printing;

namespace Fullbox.Agent.Service;

public sealed class PrinterController
{
    public IReadOnlyList<string> ListPrinters()
    {
        return ListPrinterDetails().Select(item => item.Name).ToList();
    }

    public IReadOnlyList<PrinterDetailsSnapshot> ListPrinterDetails()
    {
        var result = new List<PrinterDetailsSnapshot>();
        var defaultPrinter = GetDefaultPrinterName();
        try
        {
            using var server = new LocalPrintServer();
            foreach (var queue in server.GetPrintQueues().OrderBy(item => item.Name, StringComparer.OrdinalIgnoreCase))
            {
                result.Add(BuildDetails(queue, defaultPrinter));
            }
        }
        catch
        {
            // ignore printer enumeration errors
        }
        return result;
    }

    public string GetDefaultPrinterName()
    {
        try
        {
            var settings = new PrinterSettings();
            return settings.PrinterName ?? "";
        }
        catch
        {
            return "";
        }
    }

    public bool Pause(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Pause();
        queue.Commit();
        return true;
    }

    public bool Resume(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Resume();
        queue.Commit();
        return true;
    }

    public bool Clear(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Purge();
        queue.Commit();
        return true;
    }

    public Dictionary<string, object> Status(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return new Dictionary<string, object> { ["found"] = false };
        var details = BuildDetails(queue, GetDefaultPrinterName());
        return new Dictionary<string, object>
        {
            ["found"] = true,
            ["name"] = details.Name,
            ["is_default"] = details.IsDefault,
            ["is_paused"] = details.IsPaused,
            ["is_offline"] = details.IsOffline,
            ["is_busy"] = details.IsBusy,
            ["is_local"] = details.IsLocal,
            ["is_network"] = details.IsNetwork,
            ["jobs"] = details.Jobs,
            ["status"] = details.Status,
        };
    }

    private static PrintQueue? GetQueue(string printerName)
    {
        if (string.IsNullOrWhiteSpace(printerName))
        {
            return null;
        }
        try
        {
            var server = new LocalPrintServer();
            return server.GetPrintQueue(printerName);
        }
        catch
        {
            return null;
        }
    }

    private static PrinterDetailsSnapshot BuildDetails(PrintQueue queue, string defaultPrinter)
    {
        try
        {
            queue.Refresh();
        }
        catch
        {
            // ignore refresh errors
        }

        var portName = SafeRead(() => queue.QueuePort?.Name ?? "", "");
        var statusText = SafeRead(() => queue.QueueStatus == 0 ? "" : queue.QueueStatus.ToString(), "");
        var isNetwork = !string.IsNullOrWhiteSpace(portName) &&
            (portName.StartsWith(@"\\", StringComparison.OrdinalIgnoreCase)
            || portName.StartsWith("IP_", StringComparison.OrdinalIgnoreCase)
            || portName.Contains("TCP", StringComparison.OrdinalIgnoreCase));

        return new PrinterDetailsSnapshot(
            queue.Name,
            string.Equals(queue.Name, defaultPrinter, StringComparison.OrdinalIgnoreCase),
            SafeRead(() => queue.IsPaused, false),
            SafeRead(() => queue.IsOffline, false),
            SafeRead(() => queue.IsBusy || queue.NumberOfJobs > 0, false),
            !isNetwork,
            isNetwork,
            SafeRead(() => queue.NumberOfJobs, 0),
            statusText
        );
    }

    private static T SafeRead<T>(Func<T> read, T fallback)
    {
        try
        {
            return read();
        }
        catch
        {
            return fallback;
        }
    }
}

public sealed record PrinterDetailsSnapshot(
    string Name,
    bool IsDefault,
    bool IsPaused,
    bool IsOffline,
    bool IsBusy,
    bool IsLocal,
    bool IsNetwork,
    int Jobs,
    string Status
);
