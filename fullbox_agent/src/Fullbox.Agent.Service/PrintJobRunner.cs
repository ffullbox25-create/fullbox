using System.Drawing;
using System.Drawing.Printing;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class PrintJobRunner
{
    private readonly AgentRuntime _runtime;
    private readonly PrintAgentClient _client;
    private readonly PrintStatusStore _statusStore;
    private readonly ILogger<PrintJobRunner> _logger;

    public PrintJobRunner(
        AgentRuntime runtime,
        PrintAgentClient client,
        PrintStatusStore statusStore,
        ILogger<PrintJobRunner> logger
    )
    {
        _runtime = runtime;
        _client = client;
        _statusStore = statusStore;
        _logger = logger;
    }

    public async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            _runtime.ReloadConfig();
            _client.Reload();

            if (string.IsNullOrWhiteSpace(_runtime.Config.PrintToken))
            {
                _statusStore.Set("disabled", false, "Токен печати не задан");
                await Task.Delay(TimeSpan.FromSeconds(5), token);
                continue;
            }

            var response = await _client.GetNextJobAsync(token);
            if (response == null)
            {
                _statusStore.Set("error", false, "Сервер печати не ответил");
                await Task.Delay(TimeSpan.FromSeconds(2), token);
                continue;
            }
            if (response.Paused)
            {
                _statusStore.Set("paused", false, "Печать на паузе");
                await Task.Delay(TimeSpan.FromSeconds(_runtime.Config.PrintPollIntervalSec), token);
                continue;
            }
            if (!response.HasJob || response.Job == null)
            {
                _statusStore.Set("idle", true, "Очередь пуста");
                await Task.Delay(TimeSpan.FromSeconds(_runtime.Config.PrintPollIntervalSec), token);
                continue;
            }

            var job = response.Job;
            var success = false;
            string error = "";
            _statusStore.Set("printing", false, $"Печать задания #{job.Id}", job.Id, job.PrinterName);
            try
            {
                success = PrintLabel(job);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Print failed");
                error = ex.Message;
            }

            if (success)
            {
                _statusStore.Set("printed", true, $"Напечатано задание #{job.Id}", job.Id, job.PrinterName);
            }
            else
            {
                var message = string.IsNullOrWhiteSpace(error) ? "Ошибка печати" : error;
                _statusStore.Set("error", false, message, job.Id, job.PrinterName, error);
            }

            await _client.CompleteJobAsync(
                job.Id,
                success ? "printed" : "failed",
                success ? "" : (string.IsNullOrWhiteSpace(error) ? "print_failed" : error),
                token
            );
        }
    }

    private bool PrintLabel(PrintJob job)
    {
        if (string.IsNullOrWhiteSpace(job.PrinterName))
        {
            return false;
        }
        var imageBytes = Convert.FromBase64String(job.LabelPngBase64);
        using var stream = new MemoryStream(imageBytes);
        using var image = Image.FromStream(stream);

        using var doc = new PrintDocument();
        doc.PrinterSettings.PrinterName = job.PrinterName;
        if (!doc.PrinterSettings.IsValid)
        {
            return false;
        }
        var labelWidthMm = Math.Max(1, job.LabelWidthMm);
        var labelHeightMm = Math.Max(1, job.LabelHeightMm);
        var isLandscape = labelWidthMm > labelHeightMm;
        var width = MmToHundredths(labelWidthMm);
        var height = MmToHundredths(labelHeightMm);
        doc.DefaultPageSettings.PaperSize = new PaperSize("Label", width, height);
        doc.DefaultPageSettings.Landscape = isLandscape;
        doc.DefaultPageSettings.Margins = new Margins(0, 0, 0, 0);
        doc.PrintPage += (_, args) =>
        {
            var graphics = args.Graphics!;
            var bounds = args.PageBounds;
            var pageIsLandscape = bounds.Width > bounds.Height;
            if (pageIsLandscape == isLandscape || bounds.Width == bounds.Height)
            {
                graphics.DrawImage(image, bounds);
            }
            else
            {
                // Some printer drivers keep the previous orientation profile between sizes.
                if (isLandscape)
                {
                    graphics.TranslateTransform(bounds.Width, 0);
                    graphics.RotateTransform(90);
                }
                else
                {
                    graphics.TranslateTransform(0, bounds.Height);
                    graphics.RotateTransform(-90);
                }
                graphics.DrawImage(image, 0, 0, bounds.Height, bounds.Width);
                graphics.ResetTransform();
            }
            args.HasMorePages = false;
        };
        doc.Print();
        return true;
    }

    private static int MmToHundredths(int mm)
    {
        var inches = mm / 25.4;
        return Math.Max(1, (int)Math.Round(inches * 100));
    }
}
