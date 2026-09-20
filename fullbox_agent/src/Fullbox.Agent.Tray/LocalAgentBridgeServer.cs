using System.Net;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Tray;

public sealed class LocalAgentBridgeServer : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    private readonly Func<DiagnosticsSnapshot> _snapshotFactory;
    private readonly ILogger<LocalAgentBridgeServer> _logger;
    private readonly HttpListener _listener = new();
    private CancellationTokenSource? _cts;
    private Task? _loopTask;

    public LocalAgentBridgeServer(Func<DiagnosticsSnapshot> snapshotFactory, ILogger<LocalAgentBridgeServer> logger)
    {
        _snapshotFactory = snapshotFactory;
        _logger = logger;
        _listener.Prefixes.Add("http://127.0.0.1:17841/");
        _listener.Prefixes.Add("http://localhost:17841/");
    }

    public void Start()
    {
        if (_listener.IsListening)
        {
            return;
        }

        _cts = new CancellationTokenSource();
        _listener.Start();
        _loopTask = Task.Run(() => RunAsync(_cts.Token));
    }

    public void Stop()
    {
        try
        {
            _cts?.Cancel();
        }
        catch
        {
            // ignore stop errors
        }

        try
        {
            if (_listener.IsListening)
            {
                _listener.Stop();
            }
        }
        catch
        {
            // ignore stop errors
        }

        try
        {
            _loopTask?.Wait(TimeSpan.FromSeconds(2));
        }
        catch
        {
            // ignore stop errors
        }
    }

    public void Dispose()
    {
        Stop();
        _listener.Close();
        _cts?.Dispose();
    }

    private async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            HttpListenerContext? context = null;
            try
            {
                context = await _listener.GetContextAsync();
            }
            catch (HttpListenerException)
            {
                if (token.IsCancellationRequested || !_listener.IsListening)
                {
                    break;
                }
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Local bridge accept failed");
            }

            if (context == null)
            {
                continue;
            }

            try
            {
                await HandleAsync(context);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Local bridge request failed");
                await WriteJsonAsync(
                    context.Response,
                    new Dictionary<string, object>
                    {
                        ["ok"] = false,
                        ["error"] = "bridge_failed",
                    },
                    HttpStatusCode.InternalServerError
                );
            }
        }
    }

    private Task HandleAsync(HttpListenerContext context)
    {
        var request = context.Request;
        var path = request.Url?.AbsolutePath?.Trim() ?? "/";
        if (!string.Equals(request.HttpMethod, "GET", StringComparison.OrdinalIgnoreCase))
        {
            return WriteJsonAsync(
                context.Response,
                new Dictionary<string, object>
                {
                    ["ok"] = false,
                    ["error"] = "method_not_allowed",
                },
                HttpStatusCode.MethodNotAllowed
            );
        }

        if (path == "/whoami" || path == "/status")
        {
            var snapshot = _snapshotFactory();
            return WriteJsonAsync(context.Response, BuildPayload(snapshot), HttpStatusCode.OK);
        }

        return WriteJsonAsync(
            context.Response,
            new Dictionary<string, object>
            {
                ["ok"] = false,
                ["error"] = "not_found",
            },
            HttpStatusCode.NotFound
        );
    }

    private static async Task WriteJsonAsync(HttpListenerResponse response, object payload, HttpStatusCode statusCode)
    {
        response.StatusCode = (int)statusCode;
        response.ContentType = "application/json; charset=utf-8";
        response.Headers["Cache-Control"] = "no-store";
        var bytes = JsonSerializer.SerializeToUtf8Bytes(payload, JsonOptions);
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes);
        response.OutputStream.Close();
    }

    private static Dictionary<string, object> BuildPayload(DiagnosticsSnapshot snapshot)
    {
        var payload = new Dictionary<string, object>
        {
            ["ok"] = true,
            ["source"] = "fullbox-agent-tray",
            ["agentId"] = snapshot.Config.AgentId,
            ["name"] = snapshot.Config.Name,
            ["host"] = snapshot.Config.Host,
            ["version"] = snapshot.Version,
            ["baseUrl"] = snapshot.Config.BaseUrl,
            ["scannerMode"] = snapshot.Config.ScannerMode,
            ["scannerReady"] = snapshot.Hardware.Ready,
            ["scannerReason"] = snapshot.Hardware.Reason,
            ["scannerDetails"] = snapshot.Hardware.Details,
            ["scannerPort"] = snapshot.Config.Com.PortName ?? "",
            ["defaultPrinter"] = snapshot.DefaultPrinter,
            ["printers"] = snapshot.Printers.Select(item => item.Name).ToArray(),
            ["printerDetails"] = snapshot.Printers.Select(item => new Dictionary<string, object>
            {
                ["name"] = item.Name,
                ["is_default"] = item.IsDefault,
                ["is_paused"] = item.IsPaused,
                ["is_offline"] = item.IsOffline,
                ["is_busy"] = item.IsBusy,
                ["is_local"] = item.IsLocal,
                ["is_network"] = item.IsNetwork,
                ["jobs"] = item.Jobs,
                ["status"] = item.Status,
            }).ToArray(),
            ["printStatus"] = BuildPrintStatus(snapshot.PrintStatus),
            ["now"] = snapshot.Now.ToString("O"),
        };
        return payload;
    }

    private static Dictionary<string, object> BuildPrintStatus(Fullbox.Agent.Service.PrintRuntimeStatus status)
    {
        var payload = new Dictionary<string, object>
        {
            ["state"] = status.State,
            ["ready"] = status.Ready,
            ["message"] = status.Message,
            ["updated_at"] = status.UpdatedAt.ToString("O"),
        };

        if (status.JobId.HasValue)
        {
            payload["job_id"] = status.JobId.Value;
        }
        if (!string.IsNullOrWhiteSpace(status.Printer))
        {
            payload["printer"] = status.Printer;
        }
        if (!string.IsNullOrWhiteSpace(status.Error))
        {
            payload["error"] = status.Error;
        }

        return payload;
    }
}
