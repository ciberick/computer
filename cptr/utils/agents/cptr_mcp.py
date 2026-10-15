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
    return (
        "<cptr_tools>\n"
        f'cptr exposes these tools to you through the MCP server "{MCP_SERVER_NAME}": {tools}.\n'
        "Use them for scheduled or recurring work, notifying the user, and saving durable "
        "memory. Do not emulate them with cron, sleep loops, systemd timers, or by importing "
        "cptr's Python code.\n"
        "</cptr_tools>"
    )
