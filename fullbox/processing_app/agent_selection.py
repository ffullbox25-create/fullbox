from __future__ import annotations


def deduplicate_scanner_device_agents(agents):
    result = []
    by_host: dict[str, dict] = {}
    for agent in agents:
        host_key = str(agent.host or agent.name or "").strip().casefold()
        if host_key and host_key in by_host:
            alias = str(agent.agent_id or "").strip()
            aliases = by_host[host_key]["aliases"]
            if alias and alias not in aliases:
                aliases.append(alias)
            continue
        item = {"agent": agent, "aliases": []}
        result.append(item)
        if host_key:
            by_host[host_key] = item
    return result
