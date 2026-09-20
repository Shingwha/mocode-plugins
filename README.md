# mocode-plugins

A collection of plugins for
[MoCode](https://github.com/Shingwha/mocode) — one directory per plugin,
installed straight from this repository.

| Plugin | What it adds |
|---|---|
| [kimi-search](kimi-search/) | `kimi_search` + `kimi_fetch` — web search and page fetch via the Kimi (Moonshot) search API |
| [mcp](mcp/) | MCP client — `mcp.json` servers (stdio + Streamable HTTP) as MoCode tools: `mcp__<server>__<tool>`, meta tools and `/mcp` |

## Installing

Install one plugin out of this repository without cloning anything:

```bash
mocode plugin install https://github.com/Shingwha/mocode-plugins/tree/main/kimi-search
# or, on the default branch:
mocode plugin install https://github.com/Shingwha/mocode-plugins.git#kimi-search
```

Restart MoCode, and `mocode plugin list` shows what loaded. `mocode plugin
remove <name>` takes it out again. Or clone this repository and install from
a local path:

```bash
git clone https://github.com/Shingwha/mocode-plugins
mocode plugin install ./mocode-plugins/kimi-search
```

## Trust

Installing a plugin is an act of trust: MoCode imports and runs its code on
the next start. Read what you install — every plugin here is small enough to
read in one sitting.

## Adding a plugin

One directory per plugin: a `plugin.json` manifest plus `mocode/plugin.py`
(the agent-facing code), optionally `mocode.cli/plugin.py` (terminal chrome)
and a `pyproject.toml` (its own dependencies). The full story is
[MoCode's plugin documentation](https://github.com/Shingwha/mocode/blob/master/docs/plugins.md).
