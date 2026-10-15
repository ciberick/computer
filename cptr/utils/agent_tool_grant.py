"""Grants that let external coding agents call a few cptr tools over MCP.

An agent turn gets an opaque bearer token bound to one chat: user, workspace and
model come from cptr, never from the agent. The token is stable for that binding,
so a CLI process cptr keeps alive across turns (Claude Code) keeps working, and it
expires after AGENT_TOOL_GRANT_TTL of disuse.

Which tools a grant may call is resolved on every call from the current config,
so turning the setting off cuts access immediately, even for running processes.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace

AGENT_TOOL_GRANT_TTL = 6 * 60 * 60  # seconds

ENABLED_KEY = "agents.cptr_tools.enabled"
GROUPS_KEY = "agents.cptr_tools.groups"

# Groups (from BUILTIN_TOOL_GROUPS) that may be exposed to external agents.
AGENT_TOOL_GROUPS = ("automations", "notifications", "memory")

# Automation runs must not schedule or remove automations themselves.
AUTOMATION_RUN_BLOCKED_TOOLS = {"create_automation", "delete_automation"}


@dataclass(frozen=True)
class AgentToolGrant:
    user_id: str
    chat_id: str
    workspace: str  # "" for chats without a workspace
    model_id: str  # full id, e.g. "agent:codex/gpt-6"
    builtin_tools: dict | None
    automation_run: bool
    expires_at: float


_grants: dict[str, AgentToolGrant] = {}


def _binding(grant: AgentToolGrant) -> tuple:
    return (grant.user_id, grant.chat_id, grant.workspace, grant.model_id)


def _truthy(value) -> bool:
    return value in (True, "true", "1", 1)


def issue_grant(
    *,
    user_id: str,
    chat_id: str,
    workspace: str,
    model_id: str,
    builtin_tools: dict | None,
    automation_run: bool,
) -> str:
    """Return the token for this chat binding, creating or refreshing it."""
    now = time.time()
    for token, grant in list(_grants.items()):
        if grant.expires_at <= now:
            _grants.pop(token, None)

    grant = AgentToolGrant(
        user_id=user_id,
        chat_id=chat_id,
        workspace=workspace,
        model_id=model_id,
        builtin_tools=builtin_tools,
        automation_run=automation_run,
        expires_at=now + AGENT_TOOL_GRANT_TTL,
    )
    for token, existing in _grants.items():
        if _binding(existing) == _binding(grant):
            _grants[token] = grant
            return token

    token = secrets.token_urlsafe(32)
    _grants[token] = grant
    return token


def resolve_grant(token: str) -> AgentToolGrant | None:
    grant = _grants.get(token)
    if grant is None:
        return None
    now = time.time()
    if grant.expires_at <= now:
        _grants.pop(token, None)
        return None
    _grants[token] = replace(grant, expires_at=now + AGENT_TOOL_GRANT_TTL)
    return grant


async def agent_tools_enabled() -> bool:
    from cptr.models import Config

    return _truthy(await Config.get(ENABLED_KEY))


async def enabled_agent_groups() -> dict[str, bool]:
    from cptr.models import Config

    raw = await Config.get(GROUPS_KEY)
    groups = raw if isinstance(raw, dict) else {}
    return {group: groups.get(group) is not False for group in AGENT_TOOL_GROUPS}


async def granted_tool_names(
    *, workspace: str, builtin_tools: dict | None, automation_run: bool
) -> list[str]:
    """Tool names an agent may call now, following the same rules as API models."""
    if not await agent_tools_enabled():
        return []

    from cptr.models import Config
    from cptr.utils.tools import (
        BUILTIN_TOOL_GROUPS,
        GLOBAL_CHAT_DISABLED_TOOLS,
        disabled_builtin_tool_names,
    )

    groups = await enabled_agent_groups()
    names = [
        name for group in AGENT_TOOL_GROUPS if groups[group] for name in BUILTIN_TOOL_GROUPS[group]
    ]

    blocked = disabled_builtin_tool_names(builtin_tools)
    if not workspace:
        blocked |= GLOBAL_CHAT_DISABLED_TOOLS
    if (await Config.get("memory.enabled")) in (False, "false", "0"):
        blocked.add("update_memory")
    if automation_run:
        blocked |= AUTOMATION_RUN_BLOCKED_TOOLS
    return [name for name in names if name not in blocked]


async def grant_tool_names(grant: AgentToolGrant) -> list[str]:
    return await granted_tool_names(
        workspace=grant.workspace,
        builtin_tools=grant.builtin_tools,
        automation_run=grant.automation_run,
    )
