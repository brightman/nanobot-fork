"""Subagent manager for background task execution."""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool


@dataclass
class SubagentDefinition:
    """Claude-style subagent definition loaded from markdown frontmatter."""

    name: str
    description: str = ""
    system_prompt: str = ""
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    model: str | None = None
    max_turns: int = 15
    permission_mode: str | None = None
    memory: str | None = None
    isolation: str = "shared"
    spawn: bool = False
    skills: list[str] | None = None
    mcp: bool | list[str] = False
    source_file: str | None = None


class SubagentManager:
    """
    Manages background subagent execution.
    
    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """
    
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        parent_tools: ToolRegistry | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.parent_tools = parent_tools
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._defs_cache: dict[str, SubagentDefinition] | None = None
        self._chat_histories: dict[str, list[dict[str, Any]]] = {}
    
    async def spawn(
        self,
        task: str,
        agent: str | None = None,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.
        
        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.
        
        Returns:
            Status message indicating the subagent was started.
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or (agent if agent else task[:30] + ("..." if len(task) > 30 else ""))
        
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }
        
        # Create background task
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, agent=agent)
        )
        self._running_tasks[task_id] = bg_task
        
        # Cleanup when done
        bg_task.add_done_callback(lambda _: self._running_tasks.pop(task_id, None))
        
        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        agent_hint = f" with profile '{agent}'" if agent else ""
        return f"Subagent [{display_label}] started{agent_hint} (id: {task_id}). I'll notify you when it completes."
    
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        agent: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        
        try:
            final_result, status = await self._execute_subagent_task(
                task_id=task_id,
                task=task,
                agent=agent,
            )
            
            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, status)
            
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def run_once(
        self,
        task: str,
        agent: str | None = None,
    ) -> tuple[str, str]:
        """Run a subagent task synchronously and return (status, result)."""
        return await self._execute_subagent_task(
            task_id=f"direct-{str(uuid.uuid4())[:8]}",
            task=task,
            agent=agent,
        )

    async def chat_turn(
        self,
        agent: str,
        user_input: str,
        session_id: str = "cli",
    ) -> tuple[str, str]:
        """Run one conversational turn with a selected subagent profile."""
        subdef = self.get_definition(agent)
        if not subdef:
            return f"Subagent profile not found: {agent}", "error"

        key = f"{agent}:{session_id}"
        if user_input.strip().lower() == "/new":
            self._chat_histories.pop(key, None)
            return "Subagent chat session reset.", "ok"

        history = self._chat_histories.get(key)
        if not history:
            history = [
                {"role": "system", "content": self._build_subagent_prompt("", subdef=subdef)},
            ]

        history.append({"role": "user", "content": user_input})
        result, status, updated = await self._run_dialog(
            task_id=f"chat-{str(uuid.uuid4())[:8]}",
            messages=history,
            subdef=subdef,
        )
        self._chat_histories[key] = updated[-80:]
        return result, status

    async def _execute_subagent_task(
        self,
        task_id: str,
        task: str,
        agent: str | None = None,
    ) -> tuple[str, str]:
        """Execute subagent task and return (result, status)."""
        subdef = self.get_definition(agent) if agent else None

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._build_subagent_prompt(task, subdef=subdef)},
            {"role": "user", "content": task},
        ]
        result, status, _ = await self._run_dialog(task_id=task_id, messages=messages, subdef=subdef)
        return result, status

    async def _run_dialog(
        self,
        task_id: str,
        messages: list[dict[str, Any]],
        subdef: SubagentDefinition | None,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Run a dialog loop and return (result, status, updated_messages)."""
        tools = self._build_tool_registry(subdef)

        max_iterations = max(1, (subdef.max_turns if subdef else 15))
        model = subdef.model if (subdef and subdef.model) else self.model
        iteration = 0
        final_result: str | None = None

        while iteration < max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=tools.get_definitions(),
                model=model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                })

                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                    result = await tools.execute(tool_call.name, tool_call.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    })
            else:
                final_result = response.content
                break

        if not isinstance(final_result, str) or not final_result.strip():
            final_result = (
                "Task completed, but the subagent returned an empty response. "
                "Try refining the task or checking provider/model availability."
            )
        messages.append({"role": "assistant", "content": final_result})
        return final_result, "ok", messages
    
    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"
        
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""
        
        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )
        
        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])
    
    def _build_subagent_prompt(self, task: str, subdef: SubagentDefinition | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        custom = (subdef.system_prompt.strip() if subdef and subdef.system_prompt else "")

        skill_context = self._load_configured_skills(subdef)

        if custom:
            return f"""# Subagent

## Current Time
{now} ({tz})

{custom}

{skill_context}
"""

        base_prompt = f"""# Subagent

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}
Skills are available at: {self.workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""
        if skill_context:
            return base_prompt + "\n\n" + skill_context
        return base_prompt

    def list_definitions(self) -> list[SubagentDefinition]:
        """List available subagent profiles from workspace/agents."""
        return sorted(self._load_definitions().values(), key=lambda d: d.name)

    def get_definition(self, name: str | None) -> SubagentDefinition | None:
        if not name:
            return None
        return self._load_definitions().get(name.strip())

    async def route(self, task: str) -> tuple[SubagentDefinition | None, str]:
        """
        Claude-style routing: let the main model decide whether to delegate to a
        subagent or keep execution in main agent. Falls back to rule-based routing.
        """
        defs = self.list_definitions()
        if not defs:
            return None, "no-subagents"

        task_text = (task or "").strip()
        if not task_text:
            return None, "empty-task"

        chosen, reason, decided = await self._route_by_llm(task_text, defs)
        if decided:
            return chosen, reason

        # Fallback keeps deterministic behavior when provider output is malformed
        # or routing model is unavailable.
        fallback = self._route_by_rules(task_text, defs)
        if fallback is not None:
            return fallback, "rules-fallback"
        return None, reason or "main"

    async def _route_by_llm(
        self,
        task: str,
        defs: list[SubagentDefinition],
    ) -> tuple[SubagentDefinition | None, str, bool]:
        """Use provider semantic judgment to decide MAIN vs subagent."""
        candidates = []
        for d in defs:
            candidates.append({
                "name": d.name,
                "description": d.description or "",
                "tools": d.tools or [],
            })

        system_prompt = (
            "You are a routing planner for subagents.\n"
            "Decide whether the MAIN agent should handle the task or delegate to ONE subagent.\n"
            "Rules:\n"
            "1) Prefer MAIN for simple chat, broad strategy, or ambiguous requests.\n"
            "2) Delegate only when one subagent is clearly specialized.\n"
            "3) If uncertain, choose MAIN.\n"
            "Output JSON only: {\"decision\":\"main|subagent\",\"agent\":\"<name or empty>\",\"reason\":\"short\"}"
        )
        user_prompt = json.dumps(
            {"task": task, "subagents": candidates},
            ensure_ascii=False,
        )

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
                model=self.model,
                temperature=0.0,
                max_tokens=256,
            )
        except Exception as e:
            logger.debug("Subagent LLM routing failed: {}", e)
            return None, "llm-routing-error", False

        raw = (response.content or "").strip()
        parsed = self._parse_router_json(raw)
        if not parsed:
            return None, "llm-routing-parse-failed", False

        decision = str(parsed.get("decision") or "").strip().lower()
        reason = str(parsed.get("reason") or "").strip() or "llm-routing"
        if decision == "main":
            return None, reason, True
        if decision != "subagent":
            return None, reason, False

        chosen_name = str(parsed.get("agent") or "").strip()
        if not chosen_name:
            return None, reason, False
        for d in defs:
            if d.name == chosen_name:
                return d, reason, True
        return None, f"{reason} (unknown-agent:{chosen_name})", False

    @staticmethod
    def _parse_router_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        # tolerate fenced JSON blocks
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else None
        except Exception:
            pass
        # fallback: first {...} block
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _route_by_rules(self, task: str, defs: list[SubagentDefinition]) -> SubagentDefinition | None:
        """Deterministic backup router."""
        task_text = (task or "").strip().lower()
        if not task_text:
            return None

        task_tokens = self._tokens(task_text)
        if not task_tokens:
            return None

        best: tuple[int, SubagentDefinition] | None = None
        for d in defs:
            hay = " ".join(
                [d.name or "", d.description or "", d.system_prompt or ""]
            ).lower()
            hay_tokens = self._tokens(hay)
            overlap = len(task_tokens & hay_tokens)
            score = overlap

            # Strong boosts for explicit mentions
            if d.name.lower() in task_text:
                score += 6
            # Prefer profile when task begins with classic research verbs
            if any(v in task_tokens for v in {"research", "investigate", "analyze", "analyse"}):
                if any(v in hay_tokens for v in {"research", "investigate", "analyze", "analyse"}):
                    score += 3

            if best is None or score > best[0]:
                best = (score, d)

        # Require a minimum confidence to avoid over-routing.
        if best and best[0] >= 2:
            return best[1]
        return None

    def _load_definitions(self) -> dict[str, SubagentDefinition]:
        if self._defs_cache is not None:
            return self._defs_cache

        defs: dict[str, SubagentDefinition] = {}
        global_agents_dir = Path.home() / ".nanobot" / "agents"
        workspace_agents_dir = self.workspace / "agents"

        # Load global first, then workspace override by same name.
        all_files: list[Path] = []
        if global_agents_dir.exists():
            all_files.extend(sorted(global_agents_dir.glob("*.md")))
        if workspace_agents_dir.exists():
            all_files.extend(sorted(workspace_agents_dir.glob("*.md")))
        if not all_files:
            self._defs_cache = defs
            return defs

        for path in all_files:
            try:
                raw = path.read_text(encoding="utf-8")
                meta, body = self._parse_frontmatter(raw)
                name = str(meta.get("name") or path.stem).strip()
                if not name:
                    continue
                tools = self._coerce_str_list(meta.get("tools"))
                disallowed = self._coerce_str_list(meta.get("disallowedTools") or meta.get("disallowed_tools"))
                max_turns = self._coerce_int(meta.get("maxTurns"), default=15)
                defs[name] = SubagentDefinition(
                    name=name,
                    description=str(meta.get("description") or "").strip(),
                    system_prompt=(str(meta.get("systemPrompt") or "").strip() or body.strip()),
                    tools=tools,
                    disallowed_tools=disallowed,
                    model=str(meta.get("model")).strip() if meta.get("model") else None,
                    max_turns=max_turns,
                    permission_mode=str(meta.get("permissionMode")).strip() if meta.get("permissionMode") else None,
                    memory=str(meta.get("memory")).strip() if meta.get("memory") else None,
                    isolation=str(meta.get("isolation") or "shared").strip() or "shared",
                    spawn=bool(meta.get("spawn", False)),
                    skills=self._coerce_str_list(meta.get("skills")),
                    mcp=meta.get("mcp", False),
                    source_file=str(path),
                )
            except Exception as e:
                logger.warning("Failed to load subagent profile {}: {}", path, e)

        self._defs_cache = defs
        return defs

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        if not content.startswith("---\n"):
            return {}, content
        end = content.find("\n---\n", 4)
        if end == -1:
            return {}, content
        frontmatter = content[4:end].strip()
        body = content[end + 5:]

        # Prefer YAML parsing for Claude-style frontmatter (lists, block strings, etc.).
        try:
            import yaml  # type: ignore

            parsed = yaml.safe_load(frontmatter)
            if isinstance(parsed, dict):
                return parsed, body
        except Exception:
            pass

        meta: dict[str, Any] = {}
        for raw_line in frontmatter.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip()
            val = v.strip()
            if val.startswith("[") and val.endswith("]"):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        meta[key] = parsed
                        continue
                except Exception:
                    pass
            if val.lower() in {"true", "false"}:
                meta[key] = (val.lower() == "true")
            elif re.fullmatch(r"-?\d+", val):
                meta[key] = int(val)
            elif (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
                meta[key] = val[1:-1]
            else:
                meta[key] = val
        return meta, body

    @staticmethod
    def _coerce_int(v: Any, default: int) -> int:
        try:
            i = int(v)
            return i if i > 0 else default
        except Exception:
            return default

    @staticmethod
    def _coerce_str_list(v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",")]
            items = []
            for p in parts:
                p = p.strip().strip("[]").strip("'").strip('"')
                if p:
                    items.append(p)
            return items or None
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            return items or None
        return None

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())}

    @staticmethod
    def _tool_alias(name: str) -> str:
        raw = (name or "").strip().lower()
        aliases = {
            "read": "read_file",
            "write": "write_file",
            "edit": "edit_file",
            "list": "list_dir",
            "ls": "list_dir",
            "search": "web_search",
            "fetch": "web_fetch",
            "exec": "exec",
            "shell": "exec",
        }
        return aliases.get(raw, raw)

    def _build_tool_registry(self, subdef: SubagentDefinition | None) -> ToolRegistry:
        registry = ToolRegistry()
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        candidates = {
            "read_file": ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir),
            "write_file": WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir),
            "edit_file": EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir),
            "list_dir": ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir),
            "exec": ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
            ),
            "web_search": WebSearchTool(api_key=self.brave_api_key),
            "web_fetch": WebFetchTool(),
        }
        if subdef and subdef.spawn:
            # Explicitly opt-in only. Default is disabled to match Claude-style subagent behavior.
            candidates["spawn"] = SpawnTool(manager=self)

        # MCP tools are opt-in via subagent config.
        if subdef and self.parent_tools and subdef.mcp:
            requested = subdef.mcp
            for name in self.parent_tools.tool_names:
                if not name.startswith("mcp_"):
                    continue
                if self._mcp_selected(name, requested):
                    tool = self.parent_tools.get(name)
                    if tool:
                        candidates[name] = tool

        allow = {self._tool_alias(x) for x in (subdef.tools or [])} if subdef and subdef.tools else set(candidates.keys())
        deny = {self._tool_alias(x) for x in (subdef.disallowed_tools or [])} if subdef and subdef.disallowed_tools else set()
        enabled = [name for name in candidates.keys() if name in allow and name not in deny]

        for name in enabled:
            registry.register(candidates[name])
        return registry

    @staticmethod
    def _mcp_selected(tool_name: str, requested: bool | list[str]) -> bool:
        """Match configured MCP selection against a concrete MCP tool name."""
        if requested is True:
            return True
        if isinstance(requested, list):
            wanted = {str(x).strip() for x in requested if str(x).strip()}
            if not wanted:
                return False
            if "*" in wanted or "all" in wanted:
                return True
            if tool_name in wanted:
                return True
            # Support server-level selection: mcp_<server>_*
            for item in wanted:
                if tool_name.startswith(f"mcp_{item}_"):
                    return True
        return False

    def _load_configured_skills(self, subdef: SubagentDefinition | None) -> str:
        """Load configured skill markdowns and append as context for subagent."""
        if not subdef or not subdef.skills:
            return ""
        blocks: list[str] = []
        for skill_name in subdef.skills:
            path = self.workspace / "skills" / skill_name / "SKILL.md"
            if not path.exists():
                logger.warning("Subagent skill not found: {}", path)
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
                blocks.append(f"### Skill: {skill_name}\n\n{content}")
            except Exception as e:
                logger.warning("Failed to read subagent skill {}: {}", path, e)
        if not blocks:
            return ""
        return "## Configured Skills\n\n" + "\n\n---\n\n".join(blocks)
    
    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
