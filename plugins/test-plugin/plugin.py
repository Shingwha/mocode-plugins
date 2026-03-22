"""Tool Debug Plugin - Shows tool call details for debugging"""

from mocode.plugins import (
    Plugin,
    PluginMetadata,
    HookBase,
    HookContext,
    HookPoint,
)


class ToolBeforeHook(HookBase):
    """Hook that logs tool calls before execution"""

    _name = "tool-debug-before"
    _hook_point = HookPoint.TOOL_BEFORE_RUN
    _priority = 5  # Run very early

    def execute(self, context: HookContext) -> HookContext:
        tool_name = context.data.get("name", "")
        tool_args = context.data.get("args", {})

        # Format output
        print(f"\n{'='*60}")
        print(f"[ToolDebug] TOOL_BEFORE_RUN: {tool_name}")
        print(f"[ToolDebug] Args: {tool_args}")
        print(f"{'='*60}\n")

        return context


class ToolAfterHook(HookBase):
    """Hook that logs tool results after execution"""

    _name = "tool-debug-after"
    _hook_point = HookPoint.TOOL_AFTER_RUN
    _priority = 100  # Run very late

    def execute(self, context: HookContext) -> HookContext:
        tool_name = context.data.get("name", "")
        result = context.data.get("result", "")
        tool_args = context.data.get("args", {})

        # Show command for bash
        command = tool_args.get("command", "") if tool_name == "bash" else ""

        # Format output
        print(f"\n{'='*60}")
        print(f"[ToolDebug] TOOL_AFTER_RUN: {tool_name}")
        if command:
            print(f"[ToolDebug] Command: {command}")
        print(f"[ToolDebug] Result ({len(result)} chars): {result[:300]}{'...' if len(result) > 300 else ''}")
        print(f"{'='*60}\n")

        return context


class ToolDebugPlugin(Plugin):
    """Plugin for debugging tool calls - shows detailed info before/after tool execution"""

    def __init__(self):
        self.metadata = PluginMetadata(
            name="test-plugin",
            version="1.0.0",
            description="Debug plugin - shows tool call details",
            author="mocode",
        )

    def on_load(self) -> None:
        print("[ToolDebug] Plugin loaded")

    def on_enable(self) -> None:
        print("[ToolDebug] Plugin enabled - will show tool call details")

    def on_disable(self) -> None:
        print("[ToolDebug] Plugin disabled")

    def on_unload(self) -> None:
        print("[ToolDebug] Plugin unloaded")

    def get_hooks(self) -> list[HookBase]:
        return [
            ToolBeforeHook(),
            ToolAfterHook(),
        ]


# Entry point for plugin discovery
plugin_class = ToolDebugPlugin
