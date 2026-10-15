"""Hand cptr's agent MCP server to coding agent CLIs, in each CLI's own config shape."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

MCP_SERVER_NAME = "cptr"
TOKEN_ENV_VAR = "CPTR_MCP_TOKEN"


@dataclass(frozen=True)
class CptrMcpSpec:
    url: str
    token: str
    tools: tuple[str, ...]

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def claude_code_servers(self) -> dict[str, dict]:
        return {MCP_SERVER_NAME: {"type": "http", "url": self.url, "headers": self.auth_headers()}}

    def claude_code_allowed_tools(self) -> list[str]:
        return [f"mcp__{MCP_SERVER_NAME}__{name}" for name in self.tools]

    def acp_http_server(self) -> dict:
        """An ACP ``McpServer`` entry (HTTP transport) for session/new and session/load."""
        return {
            "type": "http",
            "name": MCP_SERVER_NAME,
            "url": self.url,
            "headers": [{"name": k, "value": v} for k, v in self.auth_headers().items()],
        }

    def codex_config_args(self, *, auto_approve: bool) -> list[str]:
        """`-c` overrides for `codex app-server`; the token travels in TOKEN_ENV_VAR."""
        prefix = f"mcp_servers.{MCP_SERVER_NAME}"
        args = [
            "-c",
            f"{prefix}.url={json.dumps(self.url)}",
            "-c",
            f"{prefix}.bearer_token_env_var={json.dumps(TOKEN_ENV_VAR)}",
        ]
        if auto_approve:
            args += ["-c", f'{prefix}.default_tools_approval_mode="approve"']
        return args


WILDCARD_HOSTS = {"", "0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}


def cptr_bind_host() -> str:
    """Address agents reach cptr on: loopback, unless cptr is bound to one specific address."""
    host = os.environ.get("CPTR_HOST", "").strip()
    return "127.0.0.1" if host in WILDCARD_HOSTS else host


def cptr_mcp_url() -> str:
    host = cptr_bind_host()
    if ":" in host:
        host = f"[{host}]"
    port = os.environ.get("CPTR_PORT") or "8000"
    return f"http://{host}:{port}/api/mcp/agent"


def cptr_tools_prompt(spec: CptrMcpSpec) -> str:
    tools = ", ".join(spec.tools)
    model_hint = (
        "When the user names a model for an automation, call list_models and pass its exact id "
        "as model_id; never guess an id.\n"
        if "list_models" in spec.tools
        else ""
    )
    return (
        "<cptr_tools>\n"
        f'cptr exposes these tools to you through the MCP server "{MCP_SERVER_NAME}": {tools}.\n'
        "Use them for scheduled or recurring work, notifying the user, and saving durable "
        "memory. Do not emulate them with cron, sleep loops, systemd timers, or by importing "
        "cptr's Python code.\n"
        f"{model_hint}"
        "</cptr_tools>"
    )


# For agents that cptr cannot hand its MCP server to yet, so they don't improvise one.
CPTR_TOOLS_UNAVAILABLE_PROMPT = (
    "<cptr_tools>\n"
    "cptr's automations, notifications and memory are not available to you in this chat. If "
    "the user asks for one, say so and suggest the Automations page or a Claude Code, Codex or "
    "Cursor chat. Do not emulate them with cron, sleep loops, systemd timers, or by importing "
    "cptr's Python code or writing to its database.\n"
    "</cptr_tools>"
)
