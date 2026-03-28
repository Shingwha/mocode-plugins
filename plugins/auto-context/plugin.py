"""Auto-Context Plugin - Automatically inject CLAUDE.md into system prompt"""

import os
from pathlib import Path
from typing import Any

from mocode.plugins import Plugin, PluginMetadata
from mocode.core.prompt.builder import DynamicSection, PromptContributions
from mocode.core.prompt.sections import PRIORITY_CUSTOM


class AutoContextPlugin(Plugin):
    """Auto-inject CLAUDE.md files into system prompt"""

    # Cache: path -> (mtime, content)
    _cache: dict[str, tuple[float, str]] = {}

    def __init__(self):
        self._context = None
        self.metadata = PluginMetadata(
            name="auto-context",
            version="1.0.0",
            description="Automatically inject CLAUDE.md into system prompt",
        )

    def get_prompt_sections(self) -> PromptContributions:
        return PromptContributions(
            add=[
                DynamicSection(
                    name="auto-context",
                    priority=PRIORITY_CUSTOM,
                    renderer=self._render_context,
                )
            ]
        )

    def get_commands(self) -> list:
        return [_create_context_command()]

    def _render_context(self, context: dict[str, Any]) -> str:
        workdir = ""
        if self.context:
            workdir = self.context.workdir
        if not workdir:
            workdir = context.get("cwd", "") or os.getcwd()

        parts = []
        paths = [
            Path(workdir) / "CLAUDE.md",
            Path(workdir) / ".claude" / "CLAUDE.md",
        ]

        for path in paths:
            content = self._read_cached(path)
            if content:
                parts.append(f"# Context from {path.name}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _read_cached(self, path: Path) -> str:
        """Read file with mtime-based cache"""
        try:
            if not path.exists():
                # Remove from cache if it no longer exists
                key = str(path)
                self._cache.pop(key, None)
                return ""

            stat = path.stat()
            mtime = stat.st_mtime
            key = str(path)

            cached = self._cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]

            content = path.read_text(encoding="utf-8")
            self._cache[key] = (mtime, content)
            return content
        except (OSError, UnicodeDecodeError):
            return ""


def _create_context_command():
    """Create /context command lazily."""
    from mocode.cli.commands.base import Command, CommandContext, command

    @command("/context", description="Show loaded context files")
    class ContextCommand(Command):
        """Show currently loaded context files and summaries"""

        def execute(self, ctx: CommandContext) -> bool:
            from mocode.plugins import PluginState

            info = ctx.client.get_plugin_info("auto-context")
            if not info or info.state != PluginState.ENABLED:
                if ctx.display:
                    ctx.display.error("auto-context plugin is not enabled")
                return True

            plugin = info.instance
            if not plugin:
                if ctx.display:
                    ctx.display.error("Plugin instance not available")
                return True

            workdir = ""
            if plugin.context:
                workdir = plugin.context.workdir
            if not workdir:
                workdir = os.getcwd()

            paths = [
                Path(workdir) / "CLAUDE.md",
                Path(workdir) / ".claude" / "CLAUDE.md",
            ]

            if not ctx.display:
                return True

            found = False
            for path in paths:
                content = plugin._read_cached(path)
                if content:
                    found = True
                    lines = content.split("\n")
                    preview = "\n".join(lines[:5])
                    if len(lines) > 5:
                        preview += f"\n... ({len(lines)} lines total)"
                    ctx.display.info(f"[{path}]")
                    ctx.display.command_output(preview)

            if not found:
                ctx.display.info("No context files found")
                ctx.display.command_output(
                    "  Create CLAUDE.md in your project root or .claude/ directory"
                )

            return True

    return ContextCommand()


plugin_class = AutoContextPlugin
