"""The plugin's terminal contributions — `/mcp`.

The configuration lives in files and the servers live on the other side of
handshakes, so failures happen where the user cannot see them. This command
is the one place they become visible: what is configured, what connected,
what failed and why — plus a reload that retries the ones that are not ready,
without restarting MoCode.

The two namespaces never import each other; everything here goes through the
conversation, which is the only thing they share.
"""

from __future__ import annotations

from mocode.cli import CLIPlugin
from mocode.plugins import CONTINUE, Command, CommandContext, CommandResult


def _state_of(conversation):
    """The MCP state, through the tool that holds it — the plugin itself is
    stateless by contract, so the conversation's tools are where it lives."""
    tool = conversation.tools.get("mcp_servers")
    return getattr(tool, "state", None)


async def _mcp(ctx: CommandContext) -> CommandResult:
    parts = ctx.args.split()
    state = _state_of(ctx.conversation)

    if state is None:
        await ctx.conversation.notify("The mcp plugin is not loaded.", level="warn")
        return CONTINUE

    if not parts:
        await ctx.conversation.prepare()  # the statuses are about the surface
        lines = []
        for entry in state.entries.values():
            if entry.status == "ready":
                where = entry.config.url or " ".join(
                    [entry.config.command, *entry.config.args]
                )
                lines.append(
                    f"  {entry.name:<16} ready    {len(entry.tools)} tool(s)  {where}"
                )
            elif entry.status == "failed":
                lines.append(f"  {entry.name:<16} failed   {entry.error}")
            else:
                lines.append(f"  {entry.name:<16} pending")
        body = "\n".join(lines) if lines else "  (no servers configured)"
        if state.problems:
            body += "\n\nconfiguration problems:\n" + "\n".join(
                f"  {p}" for p in state.problems
            )
        await ctx.conversation.notify(f"MCP servers\n{body}")
        return CONTINUE

    if parts[0] == "tools":
        if len(parts) != 2:
            await ctx.conversation.notify(
                "Usage: /mcp tools <server> — /mcp lists them", level="warn"
            )
            return CONTINUE
        await ctx.conversation.prepare()
        entry = state.entries.get(parts[1])
        if entry is None:
            await ctx.conversation.notify(
                f"No MCP server named {parts[1]!r} — /mcp lists them", level="warn"
            )
            return CONTINUE
        if entry.session is None:
            await ctx.conversation.notify(
                f"{entry.name} is not connected: {entry.error}", level="warn"
            )
            return CONTINUE
        lines = [f"{entry.name}: {len(entry.tools)} tool(s)"]
        for tool in entry.tools:
            lines.append(
                f"  {tool.get('name')} — {(tool.get('description') or '').strip()[:90]}"
            )
        await ctx.conversation.notify("\n".join(lines))
        return CONTINUE

    if parts[0] == "reload":
        up, registered = await state.reload_and_register(ctx.conversation.tools)
        await ctx.conversation.notify(
            f"Reloaded: {up} server(s) up, {registered} native tool(s) registered"
            " — the model hears about new ones at its next turn."
        )
        return CONTINUE

    await ctx.conversation.notify(
        "Usage: /mcp | /mcp tools <server> | /mcp reload", level="warn"
    )
    return CONTINUE


class McpCLI(CLIPlugin):
    name = "mcp.cli"
    description = "MCP in the terminal: /mcp, /mcp tools <server>, /mcp reload"

    def build(self, cli) -> None:
        cli.commands.register(
            Command("/mcp", "MCP server status, tools, reload", handler=_mcp)
        )


plugin = McpCLI()
