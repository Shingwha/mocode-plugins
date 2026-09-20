# mcp

An MCP client for [MoCode](https://github.com/Shingwha/mocode) — it reads the
`mcp.json` files and turns every server's tools into MoCode tools the model
can call.

- **stdio** servers (`npx @modelcontextprotocol/server-filesystem …`, `uvx …`)
- **Streamable HTTP** servers (`https://…/mcp`, `https://…/v1`)

Zero dependencies: stdlib `asyncio` subprocesses and `urllib`. Installing is
copying the directory — no `uv sync`, no environment of its own.

## Install

```bash
mocode plugin install https://github.com/Shingwha/mocode-plugins/tree/main/mcp
```

Restart MoCode, then `mocode plugin list` should show `mcp`. No
configuration, no keys: until an `mcp.json` exists the plugin contributes
three tools and finds nothing.

## Where the servers go

One format — the [Agent Plugins](https://agent-plugins.org) standard's
`mcp.json` — read from three places, merged by server name (a later layer
replaces an earlier server entirely):

| Layer | Path | Role |
|---|---|---|
| 1 | `<plugin-dir>/mcp.json` | what a plugin ships alongside its code |
| 2 | `~/.mocode/mcp.json` | **the global drop-in** — paste any server's block |
| 3 | `<project>/.mocode/mcp.json` | this project's overrides |

```jsonc
// ~/.mocode/mcp.json — definitions and keys in one file, one edit
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "files": { "type": "stdio", "command": "npx",
               "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
    "monid": { "type": "streamable-http", "url": "https://mcp.monid.ai/v1",
               "headers": { "Authorization": "Bearer monid_live_…" } }
  }
}
```

The global file is in your home directory and private to the machine, like
`~/.mocode/config.json` — but if you want a copy that can be shared or
committed (a project-level file, say), expand the token from the environment
instead:

```jsonc
"headers": { "Authorization": "Bearer ${MONID_TOKEN}" }   // ${VAR} — a documented extension
```

The schema is closed (`$schema` + `mcpServers`, nothing else; unknown keys
are reported and ignored). There are deliberately no knobs: disable a server
by deleting its entry, a tool with MoCode's own `/tool off mcp__…`, the whole
plugin with `"mcp": {"enabled": false}` in `~/.mocode/config.json` (the
host's standard switch — the plugin reads no config.json keys of its own).

## When things happen

`build()` registers three always-available tools; `prepare()` — MoCode's
async pass, which runs before the request surface freezes — connects every
server in parallel and registers each tool natively as
`mcp__<server>__<tool>`. So the **first turn already offers them, with
nothing announced**. A server that is slow or down costs its native tools
and nothing else:

- `mcp_servers` — what is configured, what connected, what failed and why
- `mcp_tools <server>` — one server's tools, with parameters
- `mcp_call <server> <tool> [arguments_json]` — reach a server whose native
  tools could not be registered
- `/mcp`, `/mcp tools <server>`, `/mcp reload` — the terminal side; reload
  retries the failed servers without restarting

Discovery is bounded (20 s per server); one dead server never delays the
others. A stdio server's stderr is kept (last 50 lines) and rides along with
any failure, which is usually the whole answer to "why won't it start".

## Not yet

- `sse` — the deprecated HTTP+SSE transport; a server that only speaks it is
  reported as unsupported, never silently ignored.
- `resources/*` and `prompts/*` — tools only.
- OAuth — put the bearer token in `headers`.

## 中文说明

给 MoCode 增加 MCP 客户端：读取 `mcp.json`（三层：插件自带 / `~/.mocode/mcp.json`
全局 / 项目 `.mocode/mcp.json`，按服务器名逐层覆盖），把每个服务器的工具注册成
MoCode 工具。支持 stdio（`npx`/`uvx`，Windows 的 `.cmd` 垫片已处理）与
Streamable HTTP 两种传输，零第三方依赖。

`prepare()` 阶段并行连接并在接口冻结前完成注册——**首轮对话就带有全部原生工具，
零通知**。连接失败的服务器由 `mcp_servers` / `mcp_tools` / `mcp_call` 三个元工具
和 `/mcp reload` 兜底。密钥直接写在 `~/.mocode/mcp.json`（家目录私有文件），
或用 `${VAR}` 从环境变量展开。
