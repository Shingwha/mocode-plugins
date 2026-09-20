"""The plugin's host contributions — an MCP client.

Three layers of ``mcp.json`` are read and merged by server name (a later
layer replaces an earlier server entirely):

    <plugin-dir>/mcp.json       what a plugin ships (the Agent Plugins standard)
    ~/.mocode/mcp.json          the global drop-in — paste any server's block
    <project>/.mocode/mcp.json  project-local, overrides the global

``build()`` registers three tools that always work (``mcp_servers``,
``mcp_tools``, ``mcp_call``); ``prepare()`` connects, lists each server's
tools and registers them natively as ``mcp__<server>__<tool>`` — before the
request surface freezes, so the first turn already offers them, with nothing
announced. A server that fails to connect costs its native tools and nothing
else: the meta tools and ``/mcp reload`` still reach it.

No third-party dependencies: stdlib ``asyncio`` subprocesses and ``urllib``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from mocode.plugins import Plugin, Tool, ToolError

#: The standard's own identifier for an mcp.json file.
SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
#: What we speak at the handshake; a server may answer with a lower one.
PROTOCOL_VERSION = "2025-06-18"
#: One server's whole discovery (connect → initialize → tools/list) may take
#: this long before the rest of the conversation moves on without it.
DISCOVER_TIMEOUT = 20.0
#: One request on the wire — a handshake, a list, a call.
REQUEST_TIMEOUT = 60.0

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _expand(value: Any) -> Any:
    """``${VAR}`` from the environment — the documented escape hatch that
    keeps a shareable mcp.json free of literals. Unset variables expand to
    the empty string, like a shell."""
    if not isinstance(value, str) or "${" not in value:
        return value
    return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _safe_name(name: str) -> str:
    """A server or tool name as a MoCode tool-name segment."""
    cleaned = _SAFE_NAME.sub("_", name.strip()).strip("_")
    return cleaned or "server"


# ── the configuration layer ─────────────────────────────────────────────────


@dataclass
class ServerConfig:
    """One server, as the merged mcp.json layers describe it."""

    name: str
    type: str  # "stdio" | "streamable-http"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    source: Path | None = None  # the mcp.json that declared it

    def transport(self) -> "McpTransport":
        if self.type == "stdio":
            return StdioTransport(self)
        return HttpTransport(self)


def _parse_mcp_json(path: Path, into: dict[str, ServerConfig], problems: list[str]) -> None:
    """Read one mcp.json, validating against the standard's closed schema.

    Unknown top-level keys are reported and ignored (the loader's own
    precedent for manifests); a file that is not an object, or has no
    ``mcpServers`` object, is reported and skipped whole.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        problems.append(f"{path.name}: unreadable ({e})")
        return
    if not isinstance(data, dict):
        problems.append(f"{path.name}: not a JSON object")
        return
    unknown = sorted(set(data) - {"$schema", "mcpServers"})
    if unknown:
        problems.append(f"{path.name}: unknown field(s) {', '.join(unknown)} (ignored)")
    if "$schema" not in data:
        problems.append(f"{path.name}: no $schema (expected {SCHEMA_ID})")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        if "mcpServers" in data:
            problems.append(f"{path.name}: 'mcpServers' is not an object")
        return

    for name, entry in servers.items():
        where = f"{path.name}: server '{name}'"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object")
            continue
        kind = entry.get("type")
        if kind == "sse":
            problems.append(f"{where} uses the deprecated 'sse' transport — not supported yet")
            continue
        if kind == "streamable-http":
            url = str(entry.get("url") or "")
            if not url:
                problems.append(f"{where} has no url")
                continue
            into[name] = ServerConfig(
                name=name,
                type="streamable-http",
                url=str(_expand(url)),
                headers={
                    str(k): str(_expand(v))
                    for k, v in (entry.get("headers") or {}).items()
                },
                source=path,
            )
        elif kind == "stdio":
            command = str(entry.get("command") or "")
            if not command:
                problems.append(f"{where} has no command")
                continue
            env = {str(k): str(_expand(v)) for k, v in (entry.get("env") or {}).items()}
            if "PLUGIN_ROOT" in env or "PLUGIN_DATA" in env:
                problems.append(f"{where}: env may not set PLUGIN_ROOT/PLUGIN_DATA")
                continue
            into[name] = ServerConfig(
                name=name,
                type="stdio",
                command=str(_expand(command)),
                args=[str(_expand(a)) for a in entry.get("args") or []],
                env=env,
                cwd=str(_expand(entry.get("cwd") or "")),
                source=path,
            )
        else:
            problems.append(f"{where} has unknown type {kind!r}")
            continue


def load_servers(ctx) -> tuple[dict[str, ServerConfig], list[str]]:
    """The three mcp.json layers, merged by server name — later wins whole."""
    layers = [
        *ctx.plugin_sources,                  # what every loaded plugin ships
        ctx.home / "mcp.json",                # the global drop-in
        ctx.cwd / ".mocode" / "mcp.json",     # this project's overrides
    ]
    merged: dict[str, ServerConfig] = {}
    problems: list[str] = []
    seen: set[Path] = set()
    for path in layers:
        path = Path(path)
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        _parse_mcp_json(path, merged, problems)
    return merged, problems


# ── the transports ──────────────────────────────────────────────────────────


class McpTransport:
    """One server's wire: carry a JSON-RPC message and bring the reply back."""

    async def start(self) -> None:
        raise NotImplementedError

    async def request(self, payload: dict) -> dict:
        raise NotImplementedError

    async def notify(self, payload: dict) -> None:
        await self.request(payload)  # HTTP treats both alike; stdio overrides

    def tail(self) -> str:
        """Whatever the wire knows about why things went wrong — carried with
        a failure so it can be diagnosed from the error alone."""
        return ""

    def close(self) -> None:
        pass


def _reply_of(message: dict) -> dict:
    """A JSON-RPC reply that is an error becomes an exception here, so every
    transport reports protocol errors the same way."""
    if isinstance(message, dict) and message.get("error"):
        err = message["error"]
        raise ToolError(f"MCP error {err.get('code')}: {err.get('message')}")
    return message


class HttpTransport(McpTransport):
    """Streamable HTTP: one POST per message, ``application/json`` or SSE back.

    ``Mcp-Session-Id`` comes back with the handshake on servers that keep
    state, and rides on every request after that. Ids are the session's
    business — a request arrives here already stamped, a notification without.
    """

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._session_id = ""

    async def start(self) -> None:
        pass  # stateless by default; the handshake may give us a session id

    async def request(self, payload: dict) -> dict:
        return await asyncio.to_thread(self._post, payload)

    def close(self) -> None:
        self._session_id = ""

    def _post(self, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps(payload).encode("utf-8")
        try:
            with urlopen(
                UrlRequest(self._config.url, data=body, headers=headers, method="POST"),
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                session = response.headers.get("Mcp-Session-Id")
                raw = response.read()
        except HTTPError as e:
            raise ToolError(f"{self._config.name}: HTTP {e.code} ({e.reason})") from e
        except URLError as e:
            raise ToolError(f"{self._config.name}: cannot reach {self._config.url} ({e.reason})") from e
        except TimeoutError as e:
            raise ToolError(f"{self._config.name}: request timed out") from e
        if session:
            self._session_id = session
        if status == 202:  # a notification the server accepted with no body
            return {}
        reply = _decode(raw)
        if reply.get("id") != payload.get("id"):
            return {}  # not ours (a server-initiated notice); the caller times out
        return _reply_of(reply)


def _decode(raw: bytes) -> dict:
    """Both wire shapes MCP allows: plain JSON, or SSE with ``data:`` lines."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ToolError(f"MCP: invalid JSON response ({e})") from e
    message: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                message = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue  # an SSE keep-alive or a fragment; keep the last good one
    return message


class StdioTransport(McpTransport):
    """A local server as a subprocess: newline-delimited JSON-RPC on stdin.

    stdout is dispatched to the waiting request by id; stderr is drained
    continuously (a full pipe would deadlock the child) and the last lines
    ride along with any failure, which is what makes one debuggable.
    """

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._writers: list[asyncio.Task] = []
        self._send_lock = asyncio.Lock()
        self.stderr_tail: deque[str] = deque(maxlen=50)

    async def start(self) -> None:
        argv = self._argv()
        cwd = self._cwd()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self._config.env},
                cwd=cwd,
            )
        except (OSError, FileNotFoundError) as e:
            raise ToolError(
                f"{self._config.name}: cannot start {self._config.command!r} ({e})"
                + self._tail()
            ) from e
        self._writers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._drain_stderr()),
        ]

    def _argv(self) -> list[str]:
        """Windows ships npx/uvx as ``.cmd`` shims that exec() cannot run
        directly; resolve through ``shutil.which`` (which honours PATHEXT)
        and let cmd.exe carry the shim."""
        command = self._config.command
        if sys.platform == "win32":
            resolved = shutil.which(command)
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                return ["cmd.exe", "/c", resolved, *self._config.args]
            if resolved:
                return [resolved, *self._config.args]
            if "/" in command or "\\" in command:
                return [command, *self._config.args]
            raise ToolError(
                f"{self._config.name}: {command!r} not found on PATH"
                + self._tail()
            )
        return [command, *self._config.args]

    def _cwd(self) -> str | None:
        """``./`` and ``${PLUGIN_ROOT}`` mean the mcp.json's own directory —
        the standard's containment rule, on our three layers."""
        raw = self._config.cwd
        if not raw:
            return None
        root = str(self._config.source.parent) if self._config.source else None
        raw = raw.replace("${PLUGIN_ROOT}", root or ".")
        raw = raw.replace("${PLUGIN_DATA}", root or ".")
        if raw.startswith("./"):
            return str(Path(root or ".") / raw[2:])
        return raw

    def _tail(self) -> str:
        return f"\nstderr:\n" + "\n".join(self.stderr_tail) if self.stderr_tail else ""

    def tail(self) -> str:
        return self._tail()

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            async for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a server's stray print on stdout; ignore it
                if isinstance(message, dict) and "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
        except Exception:
            pass  # the pipe closed; pending requests fail on their timeouts

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            async for line in self._process.stderr:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    async def request(self, payload: dict) -> dict:
        if self._process is None or self._process.stdin is None:
            raise ToolError(f"{self._config.name}: not started")
        request_id = payload["id"]  # minted by the session, carried as-is
        async with self._send_lock:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            self._process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self._process.stdin.drain()
        try:
            message = await asyncio.wait_for(future, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise ToolError(
                f"{self._config.name}: no reply within {REQUEST_TIMEOUT:.0f}s"
                + self._tail()
            ) from None
        return _reply_of(message)

    async def notify(self, payload: dict) -> None:
        if self._process is None or self._process.stdin is None:
            return
        async with self._send_lock:
            self._process.stdin.write(
                (json.dumps({**payload, "jsonrpc": "2.0"}) + "\n").encode("utf-8")
            )
            await self._process.stdin.drain()

    def close(self) -> None:
        """Synchronous on purpose: the conversation's teardown is sync, and
        ``terminate()``/``cancel()`` are sync calls — no loop needed to stop
        a child process, only to have talked to it."""
        for writer in self._writers:
            writer.cancel()
        self._writers = []
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            self._process = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending = {}


# ── one server, connected ───────────────────────────────────────────────────


@dataclass
class ServerEntry:
    """A server's config plus everything a live conversation knows about it."""

    config: ServerConfig
    session: McpSession | None = None
    status: str = "pending"  # pending | ready | failed
    error: str = ""
    tools: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.config.name


class McpSession:
    """JSON-RPC over a transport: handshake once, then list and call.

    Ids are minted here — the one authority — so every transport carries a
    request already stamped and a notification bare.
    """

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._transport = config.transport()
        self._initialized = False
        self._next_id = 0

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        return await self._transport.request(payload)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._transport.notify(payload)

    async def _handshake(self) -> None:
        await self._transport.start()
        reply = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mocode-mcp", "version": "0.1.0"},
            },
        )
        # A server may answer a lower protocol version; either way, it is
        # telling us what it speaks, and the rest of the conversation uses it.
        await self._notify("notifications/initialized")
        self._initialized = True

    async def ensure_ready(self) -> None:
        if not self._initialized:
            await self._handshake()

    async def list_tools(self) -> list[dict]:
        await self.ensure_ready()
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            reply = await self._rpc("tools/list", {"cursor": cursor} if cursor else None)
            page = reply.get("result") or {}
            tools.extend(t for t in page.get("tools") or [] if isinstance(t, dict))
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    async def call(self, tool: str, arguments: dict) -> dict:
        await self.ensure_ready()
        return await self._rpc("tools/call", {"name": tool, "arguments": arguments})

    def close(self) -> None:
        self._transport.close()

    def tail(self) -> str:
        return self._transport.tail()


# ── rendering ───────────────────────────────────────────────────────────────


def _render_result(reply: dict, server: str, tool: str) -> str:
    """A tools/call reply as the model reads it. ``isError`` is the server's
    own failure report — it stays a ToolError, not a success with sad text."""
    result = reply.get("result") or {}
    parts = [
        item.get("text", "")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p).strip()
    if result.get("isError"):
        raise ToolError(f"{server}.{tool}: {text or 'the server reported an error'}")
    if not text:
        other = [k for k in result if k not in ("content", "isError")]
        text = (
            f"(no text content; fields: {', '.join(other)})"
            if other
            else "(empty response)"
        )
    return text


def _render_entry(entry: ServerEntry) -> str:
    if entry.status == "ready":
        where = entry.config.url or " ".join([entry.config.command, *entry.config.args])
        return f"{entry.name} [{entry.config.type}] — {len(entry.tools)} tool(s) — {where}"
    if entry.status == "failed":
        return f"{entry.name} [{entry.config.type}] — failed: {entry.error}"
    return f"{entry.name} [{entry.config.type}] — not discovered yet"


# ── the tools ───────────────────────────────────────────────────────────────


class McpState:
    """Everything this conversation knows about its MCP servers.

    Created in ``build()``, held by the tool instances (never by the plugin —
    one plugin object serves every conversation in the process).
    """

    def __init__(self, servers: dict[str, ServerConfig], problems: list[str]) -> None:
        self.entries = {name: ServerEntry(config) for name, config in servers.items()}
        self.problems = problems
        self.discovered = False

    async def discover(self) -> None:
        """Connect every server in parallel; each failure is recorded, not
        raised — one dead server must not cost the others their tools."""
        self.discovered = True
        await asyncio.gather(
            *(self._discover(entry) for entry in self.entries.values())
        )

    async def _discover(self, entry: ServerEntry) -> None:
        session = McpSession(entry.config)
        try:
            entry.tools = await asyncio.wait_for(session.list_tools(), DISCOVER_TIMEOUT)
            entry.session = session
            entry.status, entry.error = "ready", ""
        except asyncio.TimeoutError:
            # The bound fired before the wire could report anything itself;
            # the transport's diagnostics are the only story we have.
            entry.status = "failed"
            entry.error = (
                f"discovery timed out after {DISCOVER_TIMEOUT:.0f}s" + session.tail()
            )
            _quiet_close(session)
        except Exception as e:  # noqa: BLE001 — one server's failure stays its own
            entry.status = "failed"
            entry.error = (str(e) or type(e).__name__) + session.tail()
            _quiet_close(session)

    async def reload(self) -> list[str]:
        """Retry the servers that are not ready; returns the newly loaded
        names (whose native tools the caller registers — post-freeze, which
        is why this path announces itself and startup does not)."""
        stale = [e for e in self.entries.values() if e.status != "ready"]
        for entry in stale:
            _quiet_close(entry.session)
            entry.session, entry.tools = None, []
            entry.status = "pending"
            entry.error = ""
        await asyncio.gather(*(self._discover(entry) for entry in stale))
        return [e.name for e in stale if e.status == "ready"]

    async def reload_and_register(self, tools) -> tuple[int, int]:
        """Retry and register natively — the recovery path a terminal command
        or an application calls; the registry is live, so the tools are
        callable at once and the model hears about them on its next turn."""
        ready = await self.reload()
        registered = 0
        for name in ready:
            for spec in self.entries[name].tools:
                tools.register(McpNativeTool(self, name, spec))
                registered += 1
        return len(ready), registered

    def entry(self, name: str) -> ServerEntry:
        entry = self.entries.get(name)
        if entry is None:
            known = ", ".join(sorted(self.entries)) or "(none configured)"
            raise ToolError(f"mcp: no server named {name!r} — configured: {known}")
        return entry

    async def call(self, server: str, tool: str, arguments: dict) -> str:
        entry = self.entry(server)
        if entry.session is None:
            await self.discover_once()
            if entry.session is None:
                raise ToolError(f"mcp: server {server!r} is not available ({entry.error})")
        reply = await entry.session.call(tool, arguments)
        return _render_result(reply, server, tool)

    async def discover_once(self) -> None:
        if not self.discovered:
            await self.discover()

    def close(self) -> None:
        for entry in self.entries.values():
            _quiet_close(entry.session)


def _quiet_close(session: McpSession | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


def _parse_arguments(raw: str, server: str, tool: str) -> dict:
    text = (raw or "").strip() or "{}"
    try:
        arguments = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError(f"{server}.{tool}: arguments_json is not valid JSON ({e})") from e
    if not isinstance(arguments, dict):
        raise ToolError(f"{server}.{tool}: arguments_json must be a JSON object")
    return arguments


class McpServersTool(Tool):
    """`mcp_servers` — what is configured, what connected, what failed."""

    def __init__(self, state: McpState) -> None:
        super().__init__(
            name="mcp_servers",
            description=(
                "List the MCP servers configured for this conversation, their "
                "transport and status. Use it when a mcp__<server>__ tool is "
                "missing and you want to know why."
            ),
            params={},
            func=self._run,
            tags=frozenset({"mcp"}),
        )
        self.state = state

    async def _run(self, args: dict) -> str:
        await self.state.discover_once()
        lines = [_render_entry(e) for e in self.state.entries.values()]
        if self.state.problems:
            lines += ["", "configuration problems:"] + [
                f"  {p}" for p in self.state.problems
            ]
        return "\n".join(lines) or "No MCP servers configured."


class McpToolsTool(Tool):
    """`mcp_tools` — one server's tools, as the model needs to see them."""

    def __init__(self, state: McpState) -> None:
        super().__init__(
            name="mcp_tools",
            description=(
                "List the tools one MCP server offers, with each tool's "
                "description and parameters. Prefer the native "
                "mcp__<server>__<tool> tools when they exist; this reaches a "
                "server even when its native tools could not be registered."
            ),
            params={
                "server": {"type": "string", "description": "Server name, as mcp_servers lists it"},
            },
            func=self._run,
            tags=frozenset({"mcp"}),
            summary_key="server",
        )
        self.state = state

    async def _run(self, args: dict) -> str:
        await self.state.discover_once()
        entry = self.state.entry(args["server"])
        if entry.session is None:
            raise ToolError(f"mcp: server {entry.name!r} is not available ({entry.error})")
        if not entry.tools:
            return f"{entry.name} offers no tools."
        lines = [f"{entry.name}: {len(entry.tools)} tool(s)", ""]
        for tool in entry.tools:
            lines.append(f"- {tool.get('name')} — {(tool.get('description') or '').strip()}")
            schema = tool.get("inputSchema") or {}
            for prop, spec in (schema.get("properties") or {}).items():
                note = (spec.get("description") or "").strip()
                req = " (required)" if prop in (schema.get("required") or []) else ""
                lines.append(f"    {prop}: {spec.get('type', 'any')}{req}" + (f" — {note}" if note else ""))
        return "\n".join(lines)


class McpCallTool(Tool):
    """`mcp_call` — call any tool on any server, arguments as JSON."""

    def __init__(self, state: McpState) -> None:
        super().__init__(
            name="mcp_call",
            description=(
                "Call one tool on one MCP server. Prefer the native "
                "mcp__<server>__<tool> tools when they exist; this reaches a "
                "server even when its native tools could not be registered."
            ),
            params={
                "server": {"type": "string", "description": "Server name"},
                "tool": {"type": "string", "description": "Tool name on that server"},
                "arguments_json": {
                    "type": "string",
                    "optional": True,
                    "default": "{}",
                    "description": "The tool's arguments as a JSON object string",
                },
            },
            func=self._run,
            tags=frozenset({"mcp"}),
            summary_key="tool",
        )
        self.state = state

    async def _run(self, args: dict) -> str:
        server, tool = args["server"], args["tool"]
        arguments = _parse_arguments(args.get("arguments_json", "{}"), server, tool)
        return await self.state.call(server, tool, arguments)


def _project_params(schema: dict) -> tuple[dict[str, dict], dict[str, bool]]:
    """An MCP inputSchema as MoCode's flat parameter format.

    Simple types pass through; an array/object parameter becomes a JSON
    string parameter (MoCode's format has no nesting to offer it). A schema
    with nothing recognisable becomes one ``arguments_json`` parameter.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return (
            {
                "arguments_json": {
                    "type": "string",
                    "optional": True,
                    "default": "{}",
                    "description": "The tool's arguments as a JSON object string",
                }
            },
            {"arguments_json": True},
        )
    params: dict[str, dict] = {}
    encoded: dict[str, bool] = {}
    required = set(schema.get("required") or [])
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type", "string")
        description = (spec.get("description") or "").strip()
        entry: dict = {"type": kind if kind in ("string", "integer", "number", "boolean") else "string"}
        if kind in ("array", "object"):
            entry["type"] = "string"
            encoded[name] = True
            description = (description + " " if description else "") + f"[JSON-encoded {kind}]"
        if description:
            entry["description"] = description
        if name in required:
            entry.pop("optional", None)
            entry.pop("default", None)
        else:
            entry["optional"] = True
        params[name] = entry
    return params, encoded


class McpNativeTool(Tool):
    """One MCP tool, registered as a first-class MoCode tool."""

    def __init__(self, state: McpState, server: str, spec: dict) -> None:
        self._state = state
        self._server = server
        self._spec = spec
        params, self._encoded = _project_params(spec.get("inputSchema") or {})
        description = (spec.get("description") or "").strip() or f"MCP tool {spec.get('name')} on {server}"
        super().__init__(
            name=f"mcp__{_safe_name(server)}__{_safe_name(str(spec.get('name')))}",
            description=description,
            params=params,
            func=self._run,
            tags=frozenset({"mcp"}),
        )

    async def _run(self, args: dict) -> str:
        arguments: dict[str, Any] = {}
        for key, value in args.items():
            if self._encoded.get(key) and isinstance(value, str):
                try:
                    arguments[key] = json.loads(value)
                except json.JSONDecodeError as e:
                    raise ToolError(
                        f"{self._server}.{self._spec.get('name')}: "
                        f"parameter {key!r} must be JSON-encoded ({e})"
                    ) from e
            else:
                arguments[key] = value
        return await self._state.call(self._server, str(self._spec.get("name")), arguments)


# ── the plugin ──────────────────────────────────────────────────────────────


class McpPlugin(Plugin):
    name = "mcp"
    description = "MCP client — mcp.json servers (stdio + Streamable HTTP) as MoCode tools"

    def build(self, ctx) -> None:
        servers, problems = load_servers(ctx)
        state = McpState(servers, problems)
        # The meta tools work from the first turn whatever the servers do;
        # each holds the state, because the plugin itself must stay stateless.
        ctx.tools.register(McpServersTool(state))
        ctx.tools.register(McpToolsTool(state))
        ctx.tools.register(McpCallTool(state))

    async def prepare(self, ctx) -> None:
        tool = ctx.tools.get("mcp_servers")
        if tool is None:
            return  # disabled — nothing to discover
        state = tool.state
        await state.discover()
        for entry in state.entries.values():
            if entry.status != "ready":
                continue
            for spec in entry.tools:
                ctx.tools.register(McpNativeTool(state, entry.name, spec))

    def close(self, ctx) -> None:
        state = getattr(ctx.tools.get("mcp_servers"), "state", None)
        if state is not None:
            state.close()  # sync by design: teardown cannot await


plugin = McpPlugin()
