using Fullbox.Agent.Service;
using Microsoft.Extensions.Hosting;

Host.CreateDefaultBuilder(args)
    .UseWindowsService()
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
    .Build()
    .Run();
