# kimi-search

Web search and page fetch for [MoCode](https://github.com/Shingwha/mocode),
through the Kimi (Moonshot) search API.

Adds two tools the model can call, plus a prompt section that teaches it when
and how to search:

- **`kimi_search`** — live web search (`/v1/tools/search_pro`): returns
  passages ranked by relevance, with source, date and authority. Params:
  `query` (verbatim — no rewriting), `limit` (1–20, default 5), `sites` (up
  to 5 domains), `time_start` / `time_end` (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`).
- **`kimi_fetch`** — one URL in, clean Markdown out (`/v1/tools/fetch`). Use
  it when the search snippets are not enough.

No dependencies of the host's — the plugin ships its own environment
(`httpx`, materialised by `uv`). `mocode plugin install` sets it up in the
same breath, and `mocode plugin list` shows `own env`. If the import ever
fails on a missing package, one command fixes it:

```bash
mocode plugin sync kimi-search
```

## Install

```bash
mocode plugin install https://github.com/Shingwha/mocode-plugins/tree/main/kimi-search
```

Restart MoCode, then `mocode plugin list` should show `kimi-search`.

## Configure

The API key resolves in this order — first one found wins:

1. `plugins."kimi-search".api_key` in `~/.mocode/config.json`
2. the `KIMI_API_KEY` environment variable
3. the `MOONSHOT_API_KEY` environment variable

The simplest setup is one export in your shell profile. To configure in the
file instead:

```jsonc
{
  "plugins": {
    "kimi-search": {
      "api_key": "sk-……",
      "base_url": "https://api.moonshot.cn",   // optional, the default
      "timeout_seconds": 30                     // optional, per-request budget
    }
  }
}
```

Disable the plugin with `"enabled": false` in the same block. Without a key
the tools still appear; calling one explains exactly what to set.

## Cost

Billed **only on success**: a search counts when it returns results, a fetch
when the page yields a non-blank Markdown body — failures and empty results
are free. See
[Kimi's pricing](https://platform.kimi.com/docs/pricing/websearch).
This plugin always searches the Pro endpoint, which returns relevance-ranked
chunks instead of whole pages, precisely because that is the cheap way for an
agent to search.

## 中文说明

给 MoCode 增加 Kimi（月之暗面）联网搜索能力的插件：

- `kimi_search`：联网搜索，返回按相关性排序的内容片段（专业版接口）；
- `kimi_fetch`：把指定 URL 的网页转成干净的 Markdown。

配置最简单的方式：设置环境变量 `KIMI_API_KEY` 或 `MOONSHOT_API_KEY`；
或写入 `~/.mocode/config.json` 的 `plugins."kimi-search".api_key`（见上方
示例）。**仅成功返回结果时计费**，失败或无结果不计费；插件固定使用返回
内容片段的专业版接口以控制 token 成本。
