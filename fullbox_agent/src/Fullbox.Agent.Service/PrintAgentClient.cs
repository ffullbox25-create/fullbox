using System.Net.Http.Json;
using System.Text.Json;
using Fullbox.Agent.Shared;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class PrintAgentClient
{
    private readonly HttpClient _client = new();
    private readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };
    private readonly ILogger<PrintAgentClient> _logger;
    private readonly AgentRuntime _runtime;

    public PrintAgentClient(AgentRuntime runtime, ILogger<PrintAgentClient> logger)
    {
        _runtime = runtime;
        _logger = logger;
        ApplyHeaders();
    }

    public void Reload()
    {
        ApplyHeaders();
    }

    private void ApplyHeaders()
    {
        if (_client.BaseAddress == null)
        {
            _client.BaseAddress = new Uri(_runtime.Config.BaseUrl.TrimEnd('/') + "/");
        }
        _client.DefaultRequestHeaders.Remove("X-Print-Token");
        if (!string.IsNullOrWhiteSpace(_runtime.Config.PrintToken))
        {
            _client.DefaultRequestHeaders.Add("X-Print-Token", _runtime.Config.PrintToken);
        }
        _client.DefaultRequestHeaders.Remove("X-Print-Agent");
        var printAgent = !string.IsNullOrWhiteSpace(_runtime.Config.AgentId)
            ? _runtime.Config.AgentId
            : _runtime.Config.PrintAgentName;
        if (!string.IsNullOrWhiteSpace(printAgent))
        {
            _client.DefaultRequestHeaders.Add("X-Print-Agent", printAgent);
        }
    }

    public async Task<PrintJobResponse?> GetNextJobAsync(CancellationToken token)
    {
        if (string.IsNullOrWhiteSpace(_runtime.Config.PrintToken))
        {
            return null;
        }
        try
        {
            var response = await _client.GetAsync("orders/processing/print-jobs/next/", token);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }
            return await response.Content.ReadFromJsonAsync<PrintJobResponse>(_jsonOptions, token);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Print job fetch failed");
            return null;
        }
    }

    public async Task CompleteJobAsync(long jobId, string status, string? error, CancellationToken token)
    {
        if (string.IsNullOrWhiteSpace(_runtime.Config.PrintToken))
        {
            return;
        }
        try
        {
            var payload = new Dictionary<string, object>
            {
                ["job_id"] = jobId,
                ["status"] = status,
                ["error"] = error ?? "",
            };
            await _client.PostAsJsonAsync("orders/processing/print-jobs/complete/", payload, _jsonOptions, token);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Print job complete failed");
        }
    }
}

public sealed class PrintJobResponse
{
    public bool Ok { get; set; }
    public bool HasJob { get; set; }
    public bool Paused { get; set; }
    public PrintJob? Job { get; set; }
}

public sealed class PrintJob
{
    public long Id { get; set; }
    public string PrinterName { get; set; } = "";
    public string LabelPngBase64 { get; set; } = "";
    public int LabelWidthMm { get; set; } = 58;
    public int LabelHeightMm { get; set; } = 40;
}
