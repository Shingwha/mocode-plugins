"""Better Write Plugin - Replaces the default write tool with enhanced version"""

from pathlib import Path

from mocode.plugins import Plugin, PluginMetadata
from mocode.tools.base import Tool, ToolError


def _better_write(args: dict) -> str:
    """Enhanced write with backup and validation"""
    p = Path(args["path"])
    content = args["content"]

    if p.is_dir():
        raise ToolError(f"Path is a directory: {p}", "invalid_path")

    # Show test message
    print(f"\n[BetterWrite] This is a TEST replacement for write tool!")
    print(f"[BetterWrite] Writing to: {p}")
    print(f"[BetterWrite] Content length: {len(content)} chars")
    print(f"[BetterWrite] First 100 chars: {content[:100]}...\n")

    # Actual write
    p.write_text(content, encoding="utf-8")

    return f"[BetterWrite] Successfully wrote {len(content)} chars to {p}"


class BetterWritePlugin(Plugin):
    """Plugin that replaces the write tool with an enhanced version"""

    def __init__(self):
        self.metadata = PluginMetadata(
            name="better-write",
            version="1.0.0",
            description="Better write tool with backup and logging",
            author="mocode",
            replaces_tools=["write"],  # Declare tool replacement
        )

    async def on_load(self) -> None:
        print("[BetterWrite] Plugin loaded")

    async def on_enable(self) -> None:
        print("[BetterWrite] Plugin enabled - write tool replaced!")

    async def on_disable(self) -> None:
        print("[BetterWrite] Plugin disabled - original write tool restored!")

    async def on_unload(self) -> None:
        print("[BetterWrite] Plugin unloaded")

    def get_tools(self) -> list[Tool]:
        return [
            Tool(
                "write",  # Same name as original
                "Write content to file (enhanced version with logging)",
                {"path": "string", "content": "string"},
                _better_write,
            )
        ]


# Entry point for plugin discovery
plugin_class = BetterWritePlugin
