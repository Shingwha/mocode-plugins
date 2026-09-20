"""Web search and page fetch through Kimi's search API.

Three layers, one story: `KimiClient` speaks the API — auth, the two
endpoints, honest errors; `SearchResult` is what a search returns; two thin
tools render those for the model. The plugin ships its own environment
(`httpx` via pyproject.toml), so it runs wherever MoCode runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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


@dataclass
class SearchResult:
    """One hit, as the search API reports it.

    ``passages`` is the model's reading, decided once, here: the snippet
    plus the chunks that scored highest.
    """

    title: str = ""
    url: str = ""
    site_name: str = ""
    date: str = ""
    authority: str = ""  # the API's source-credibility grade, carried for the UI
    passages: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, item: dict) -> "SearchResult":
        scored: list[tuple[float, str]] = []
        for chunk in item.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            try:
                score = float(chunk.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            scored.append((score, text))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        passages = [text for _, text in scored[:3]]
        snippet = (item.get("snippet") or "").strip()
        if snippet and snippet not in passages:
            passages.insert(0, snippet)
        return cls(
            title=item.get("title") or "",
            url=item.get("url") or "",
            site_name=item.get("site_name") or "",
            date=item.get("date") or "",
            authority=str(item.get("authority") or ""),
            passages=passages,
        )


_STATUS_GLOSS = {
    401: f"the API key is missing or invalid — {_KEY_HINT}",
    403: "the account is not active",
    404: "the page has no extractable content",
    429: "rate limited — retry in a moment",
    504: "timed out — a larger timeout_seconds or a leaner query may help",
}


def _explain_status(status: int, body: dict) -> str:
    """HTTP {status}: the API's own words when it sent any, ours otherwise.

    The API reports its errors as ``{"error": {"message": ...}}`` — except
    401 and account-level 403, which carry no body at all.
    """
    message = ((body.get("error") or {}).get("message") or "").strip()
    if message:
        return f"HTTP {status} — {message}"
    gloss = _STATUS_GLOSS.get(status, "")
    return f"HTTP {status} — {gloss}" if gloss else f"HTTP {status}"


class KimiClient:
    """The search API and nothing else: auth, two endpoints, honest errors."""

    def __init__(self, config: dict) -> None:
        self._api_key = _api_key(config)
        self._base_url = config.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self._timeout_seconds = min(max(int(config.get("timeout_seconds") or 30), 1), 60)

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        sites: tuple = (),
        time_window: dict | None = None,
    ) -> list[SearchResult]:
        payload = {
            "text_query": query,
            "limit": min(max(int(limit), 1), 20),
            "timeout_seconds": self._timeout_seconds,
        }
        if sites:
            payload["sites"] = [str(site) for site in sites][:5]
        if time_window:
            payload["time_window"] = time_window
        body = await self._post("/v1/tools/search_pro", payload)
        return [
            SearchResult.from_payload(item)
            for item in body.get("search_results") or []
            if isinstance(item, dict)
        ]

    async def fetch(self, url: str) -> tuple[str, str]:
        """One page as ``(title, markdown)`` — the request carries the URL alone."""
        body = await self._post("/v1/tools/fetch", {"url": url})
        return body.get("title") or "", body.get("markdown") or ""

    async def _post(self, path: str, payload: dict) -> dict:
        if not self._api_key:
            raise ToolError(_KEY_HINT)
        # timeout_seconds caps the search server-side; the wire gets
        # headroom on top so httpx never fires first.
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds + 10) as client:
                response = await client.post(
                    self._base_url + path,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException as e:
            raise ToolError(f"kimi-search: request timed out ({e})") from e
        except httpx.HTTPError as e:
            raise ToolError(f"kimi-search: request failed ({e})") from e
        if response.status_code == 200:
            return response.json()
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise ToolError(f"kimi-search: {_explain_status(response.status_code, body)}")


def _render(query: str, results: list[SearchResult]) -> str:
    """The model's reading: a numbered list, passages indented under each."""
    if not results:
        return f"No results for: {query}"
    lines = [f"Web search: {query} — {len(results)} result(s)", ""]
    for i, result in enumerate(results, 1):
        meta = " · ".join(part for part in (result.site_name, result.date) if part)
        lines.append(
            f"{i}. {result.title or '(untitled)'}" + (f" ({meta})" if meta else "")
        )
        if result.url:
            lines.append(f"   {result.url}")
        for passage in result.passages:
            lines.append(f"   {passage}")
        lines.append("")
    return "\n".join(lines).rstrip()


class KimiSearchTool(Tool):
    """`kimi_search` — ask the web, get the passages that answer."""

    def __init__(self, client: KimiClient) -> None:
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
        self._client = client

    async def _run(self, args: dict) -> ToolResult:
        start = (args.get("time_start") or "").strip()
        end = (args.get("time_end") or "").strip()
        results = await self._client.search(
            args["query"],
            limit=args.get("limit") or 5,
            sites=args.get("sites") or (),
            time_window={"start": start, "end": end} if start or end else None,
        )
        return ToolResult(
            content=_render(args["query"], results),
            details={
                "result_count": len(results),
                "results": [
                    {
                        "title": result.title,
                        "url": result.url,
                        "site_name": result.site_name,
                        "date": result.date,
                        "authority": result.authority,
                    }
                    for result in results
                ],
            },
        )


class KimiFetchTool(Tool):
    """`kimi_fetch` — a URL in, clean Markdown out."""

    def __init__(self, client: KimiClient) -> None:
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
        self._client = client

    async def _run(self, args: dict) -> ToolResult:
        url = (args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"kimi_fetch: not an absolute http(s) URL: {url!r}")
        title, content = await self._client.fetch(url)
        if not content:
            raise ToolError(f"kimi_fetch: the page returned no content: {url}")
        body = f"# {title}\n\n{content}" if title else content
        return ToolResult(
            content=body, details={"title": title, "url": url, "chars": len(body)}
        )


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
        client = KimiClient(ctx.plugin_config("kimi-search"))
        # Registered even without a key: a tool that fails loudly with the fix
        # beats one that silently never appears.
        ctx.tools.register(KimiSearchTool(client))
        ctx.tools.register(KimiFetchTool(client))
        ctx.prompt_sections.append(Section("web", _guidance, priority=40))


plugin = KimiSearchPlugin()
