"""Web search and page fetch through Kimi's search API.

Two tools, one provider of facts: `kimi_search` asks the web and returns the
passages the API ranked relevant (the Pro endpoint — chunks, not whole
pages); `kimi_fetch` turns a URL into clean Markdown. Both run on the host's
own `httpx`, so there is no environment to materialise, and the API key
resolves config-first, environment-second.
"""

from __future__ import annotations

import os

import httpx

from mocode.plugins import Plugin, Section, Tool, ToolError, ToolResult

DEFAULT_BASE_URL = "https://api.moonshot.cn"

_KEY_HINT = (
    'kimi-search needs an API key — set plugins."kimi-search".api_key in '
    "~/.mocode/config.json, or export KIMI_API_KEY or MOONSHOT_API_KEY"
)


def _api_key(config: dict) -> str:
    """Config first, then the environment — the first one found wins."""
    return (
        config.get("api_key")
        or os.environ.get("KIMI_API_KEY")
        or os.environ.get("MOONSHOT_API_KEY")
        or ""
    )


async def _post(config: dict, path: str, payload: dict) -> dict:
    """One POST, one JSON body — failures come back as ToolError with a cause."""
    api_key = _api_key(config)
    if not api_key:
        raise ToolError(_KEY_HINT)
    # The API's own timeout_seconds is the search budget; the wire gets a
    # little headroom on top so httpx never fires first.
    wire_timeout = int(payload.get("timeout_seconds") or 30) + 10
    try:
        async with httpx.AsyncClient(timeout=wire_timeout) as client:
            response = await client.post(
                config.get("base_url", DEFAULT_BASE_URL).rstrip("/") + path,
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.TimeoutException as e:
        raise ToolError(f"kimi-search: request timed out ({e})") from e
    except httpx.HTTPError as e:
        raise ToolError(f"kimi-search: request failed ({e})") from e
    if response.status_code == 401:
        raise ToolError(f"kimi-search: 401 — the API key was rejected. {_KEY_HINT}")
    if response.status_code != 200:
        raise ToolError(
            f"kimi-search: HTTP {response.status_code} — {response.text[:300]}"
        )
    body = response.json()
    # The API wraps its payload; older shapes did not. Either way, what the
    # caller wants is the object under "data", or the body itself.
    return body.get("data", body) if isinstance(body, dict) else body


def _results_of(data) -> list[dict]:
    """The result list, whichever key the API settled on this quarter."""
    for key in ("search_result", "results", "items"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return value
    return data if isinstance(data, list) else []


def _passages(item: dict, limit: int = 3) -> str:
    """The snippet, plus the highest-scoring chunks — the model's reading."""
    scored: list[tuple[float, str]] = []
    for chunk in item.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        text = (chunk.get("content") or chunk.get("text") or "").strip()
        if not text:
            continue
        try:
            score = float(chunk.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        scored.append((score, text))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [text for _, text in scored[:limit]]
    snippet = (item.get("snippet") or "").strip()
    if snippet and snippet not in chosen:
        chosen.insert(0, snippet)
    return "\n   ".join(chosen)


def _format_search(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results for: {query}"
    lines = [f"Web search: {query} — {len(results)} result(s)", ""]
    for i, item in enumerate(results, 1):
        title = item.get("title") or "(untitled)"
        meta = " · ".join(
            part for part in (item.get("site_name"), item.get("date")) if part
        )
        lines.append(f"{i}. {title}" + (f" ({meta})" if meta else ""))
        if item.get("url"):
            lines.append(f"   {item['url']}")
        passages = _passages(item)
        if passages:
            lines.append(f"   {passages}")
    return "\n".join(lines)


class KimiSearchTool(Tool):
    """`kimi_search` — ask the web, get the passages that answer."""

    def __init__(self, config: dict) -> None:
        super().__init__(
            name="kimi_search",
            description=(
                "Search the live web and return passages ranked by relevance. "
                "Use it for current events, versions, prices — anything you "
                "would otherwise guess. Queries are searched verbatim: make "
                "them specific (a concrete entity, a time marker, qualifiers) "
                "rather than vague keywords."
            ),
            params={
                "query": {
                    "type": "string",
                    "description": "The search query, used verbatim — no "
                    "rewriting. Combine related lookups into one query.",
                },
                "limit": {
                    "type": "integer",
                    "optional": True,
                    "default": 5,
                    "description": "Maximum results (1-20).",
                },
                "sites": {
                    "type": "array",
                    "optional": True,
                    "description": 'Restrict to up to 5 domains, e.g. '
                    '["python.org", "peps.python.org"] (treated as OR).',
                },
                "time_start": {
                    "type": "string",
                    "optional": True,
                    "description": "Only results dated from here on: "
                    "YYYY, YYYY-MM or YYYY-MM-DD.",
                },
                "time_end": {
                    "type": "string",
                    "optional": True,
                    "description": "Only results dated up to here, same format.",
                },
            },
            func=self._run,
            tags=frozenset({"web", "search"}),
            summary_key="query",
            result_key="result_count",
        )
        self._config = config

    async def _run(self, args: dict) -> ToolResult:
        payload = {
            "text_query": args["query"],
            "limit": min(max(int(args.get("limit") or 5), 1), 20),
            "timeout_seconds": int(self._config.get("timeout_seconds") or 30),
        }
        sites = args.get("sites") or []
        if sites:
            payload["sites"] = [str(site) for site in sites][:5]
        start = (args.get("time_start") or "").strip()
        end = (args.get("time_end") or "").strip()
        if start or end:
            payload["time_window"] = {"start": start, "end": end}
        data = await _post(self._config, "/v1/tools/search_pro", payload)
        results = _results_of(data)
        details = {
            "result_count": len(results),
            "results": [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "site_name": item.get("site_name", ""),
                    "date": item.get("date", ""),
                    "authority": item.get("authority"),
                }
                for item in results
                if isinstance(item, dict)
            ],
        }
        return ToolResult(
            content=_format_search(args["query"], results), details=details
        )


class KimiFetchTool(Tool):
    """`kimi_fetch` — a URL in, clean Markdown out."""

    def __init__(self, config: dict) -> None:
        super().__init__(
            name="kimi_fetch",
            description=(
                "Fetch one web page by URL and return its title and body as "
                "clean Markdown. Use it when search snippets are not enough — "
                "after kimi_search, on the URL that looked promising."
            ),
            params={
                "url": {
                    "type": "string",
                    "description": "Absolute http(s) URL of the page to read.",
                },
            },
            func=self._run,
            tags=frozenset({"web"}),
            summary_key="url",
            result_key="chars",
        )
        self._config = config

    async def _run(self, args: dict) -> ToolResult:
        url = (args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"kimi_fetch: not an absolute http(s) URL: {url!r}")
        data = await _post(
            self._config,
            "/v1/tools/fetch",
            {
                "url": url,
                "timeout_seconds": int(self._config.get("timeout_seconds") or 30),
            },
        )
        title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
        content = ""
        if isinstance(data, dict):
            content = (data.get("content") or data.get("markdown") or "").strip()
        if not content:
            raise ToolError(f"kimi_fetch: the page returned no content: {url}")
        body = f"# {title}\n\n{content}" if title else content
        return ToolResult(content=body, details={"title": title, "url": url, "chars": len(body)})


def _guidance(_context: dict) -> str:
    """A prompt section the model always sees — the API's own best practices."""
    return "\n".join(
        [
            "- `kimi_search` sees the live web; prefer it to guessing about "
            "current events, releases, prices or anything after your training.",
            "- Queries are searched verbatim. Write them specific: a concrete "
            "entity, a time marker, and qualifiers — not loose keywords.",
            "- Combine related follow-ups into one query; do not re-issue "
            "near-duplicate queries.",
            "- When snippets are not enough, `kimi_fetch` the promising URL "
            "instead of searching again.",
        ]
    )


class KimiSearchPlugin(Plugin):
    name = "kimi-search"
    description = "Web search and page fetch via the Kimi (Moonshot) search API"

    def build(self, ctx) -> None:
        config = ctx.plugin_config("kimi-search")
        # Registered even without a key: a tool that fails loudly with the fix
        # beats one that silently never appears.
        ctx.tools.register(KimiSearchTool(config))
        ctx.tools.register(KimiFetchTool(config))
        ctx.prompt_sections.append(Section("web", _guidance, priority=40))


plugin = KimiSearchPlugin()
