using System;
using System.IO;
using System.Text.Json;

namespace Fullbox.Agent.Shared;

public static class ConfigStore
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    public static string ConfigDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "FullboxAgent");

    public static string ConfigPath => Path.Combine(ConfigDir, "config.json");

    public static AgentConfig LoadOrCreate()
    {
        Directory.CreateDirectory(ConfigDir);
        if (!File.Exists(ConfigPath))
        {
            var fresh = new AgentConfig();
            Save(fresh);
            return fresh;
        }

        try
        {
            var json = File.ReadAllText(ConfigPath);
            var config = JsonSerializer.Deserialize<AgentConfig>(json, JsonOptions);
            config ??= new AgentConfig();
            var changed = false;
            if (string.IsNullOrWhiteSpace(config.AgentId))
            {
                config.AgentId = $"pc-{Guid.NewGuid():N}".Substring(0, 11);
                changed = true;
            }
            if (string.IsNullOrWhiteSpace(config.Name))
            {
                config.Name = Environment.MachineName;
                changed = true;
            }
            if (string.IsNullOrWhiteSpace(config.Host))
            {
                config.Host = Environment.MachineName;
                changed = true;
            }
            if (string.IsNullOrWhiteSpace(config.BaseUrl))
            {
                config.BaseUrl = "https://fullbox.ru";
                changed = true;
            }
            if (string.IsNullOrWhiteSpace(config.PrintAgentName))
            {
                config.PrintAgentName = "Fullbox Print Agent";
                changed = true;
            }
            if (string.IsNullOrWhiteSpace(config.ScannerMode))
            {
                config.ScannerMode = "Com";
                changed = true;
            }
            if (config.Keyboard == null)
            {
                config.Keyboard = new KeyboardSettings();
                changed = true;
            }
            if (changed)
            {
                Save(config);
            }
            return config;
        }
        catch
        {
            return new AgentConfig();
        }
    }

    public static void Save(AgentConfig config)
    {
        Directory.CreateDirectory(ConfigDir);
        var json = JsonSerializer.Serialize(config, JsonOptions);
        File.WriteAllText(ConfigPath, json);
    }
}
