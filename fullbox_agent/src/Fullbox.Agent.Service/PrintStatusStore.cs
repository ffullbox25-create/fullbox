namespace Fullbox.Agent.Service;

public sealed class PrintStatusStore
{
    private readonly object _sync = new();
    private PrintRuntimeStatus _status = new("idle", true, "Очередь пуста", null, "", "", DateTime.UtcNow);

    public PrintRuntimeStatus Snapshot()
    {
        lock (_sync)
        {
            return _status with { };
        }
    }

    public void Set(
        string state,
        bool ready,
        string message,
        long? jobId = null,
        string printer = "",
        string error = ""
    )
    {
        lock (_sync)
        {
            _status = new PrintRuntimeStatus(
                state?.Trim() ?? "",
                ready,
                message?.Trim() ?? "",
                jobId,
                printer?.Trim() ?? "",
                error?.Trim() ?? "",
                DateTime.UtcNow
            );
        }
    }
}

public sealed record PrintRuntimeStatus(
    string State,
    bool Ready,
    string Message,
    long? JobId,
    string Printer,
    string Error,
    DateTime UpdatedAt
);
