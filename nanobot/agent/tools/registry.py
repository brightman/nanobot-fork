"""Tool registry for dynamic tool management."""

import re
from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    """
    
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        # External tool-call name (LLM-visible) -> internal tool name
        self._name_aliases: dict[str, str] = {}
        # Internal tool name -> external tool-call name
        self._external_names: dict[str, str] = {}

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """Normalize tool name for strict providers (e.g. Bedrock/OpenRouter)."""
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "")
        # Keep name bounded and non-empty
        safe = safe[:128] or "tool"
        return safe

    def _make_unique_external_name(self, internal_name: str) -> str:
        base = self._sanitize_tool_name(internal_name)
        candidate = base
        i = 1
        while candidate in self._name_aliases and self._name_aliases[candidate] != internal_name:
            suffix = f"_{i}"
            candidate = (base[: max(1, 128 - len(suffix))] + suffix)
            i += 1
        return candidate
    
    def register(self, tool: Tool) -> None:
        """Register a tool."""
        internal_name = tool.name
        self._tools[internal_name] = tool
        external_name = self._make_unique_external_name(internal_name)
        self._name_aliases[external_name] = internal_name
        self._external_names[internal_name] = external_name
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        internal = self._name_aliases.get(name, name)
        self._tools.pop(internal, None)
        external = self._external_names.pop(internal, None)
        if external:
            self._name_aliases.pop(external, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        internal = self._name_aliases.get(name, name)
        return self._tools.get(internal)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        internal = self._name_aliases.get(name, name)
        return internal in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        defs: list[dict[str, Any]] = []
        for internal_name, tool in self._tools.items():
            schema = tool.to_schema()
            # Present provider-safe name to the LLM while keeping internal names unchanged.
            schema["function"]["name"] = self._external_names.get(internal_name, internal_name)
            defs.append(schema)
        return defs
    
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        internal_name = self._name_aliases.get(name, name)
        tool = self._tools.get(internal_name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {internal_name}: {str(e)}" + _HINT
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._name_aliases.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return self.has(name)
