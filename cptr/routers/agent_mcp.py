"""MCP endpoint that exposes a few cptr tools to external coding agents.

Speaks the MCP "Streamable HTTP" transport in its simplest form: every client
POST carries a JSON-RPC message and gets a plain JSON reply, with no SSE stream
and no session. Only tools/list and tools/call are offered, so the optional
`mcp` package is not needed.

Auth is a per-chat bearer token from cptr.utils.agent_tool_grant, accepted only
from loopback: agents always run on the same machine as cptr. The cookie auth
middleware skips this path.
"""

from __future__ import annotations

import json
import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from cptr.utils.agent_tool_grant import AgentToolGrant, grant_tool_names, resolve_grant
from cptr.utils.agents.cptr_mcp import cptr_bind_host

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MCP_PATH = "/api/mcp/agent"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
PARSE_ERROR = -32700


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _initialize_result(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    try:
        server_version = pkg_version("cptr")
    except PackageNotFoundError:
        server_version = "0"

    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "cptr", "version": server_version},
        "instructions": (
            "Tools from cptr, the app this agent runs in. Use them to schedule automations, "
            "notify the user and save durable memory."
        ),
    }


async def _tools_list(grant: AgentToolGrant) -> dict:
    from cptr.utils.tools import ALL_TOOLS, _fn_to_schema

    tools = []
    for name in await grant_tool_names(grant):
        schema = _fn_to_schema(name, ALL_TOOLS[name]["fn"])
        tools.append(
            {
                "name": name,
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            }
        )
    return {"tools": tools}


async def _tools_call(request: Request, grant: AgentToolGrant, request_id, params: dict) -> dict:
    from cptr.utils.tools import execute_tool

    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _error(request_id, INVALID_PARAMS, "tools/call needs a name and an arguments object")
    if name not in await grant_tool_names(grant):
        return _error(request_id, INVALID_PARAMS, f"Tool not available: {name}")

    context = {
        "workspace": grant.workspace,
        "user_id": grant.user_id,
        "request": request,
        "model_id": grant.model_id,
        "full_model_id": grant.model_id,
        "chat_id": grant.chat_id,
        "builtin_tools": grant.builtin_tools,
    }
    logger.info("Agent MCP call %s (chat %s, model %s)", name, grant.chat_id, grant.model_id)
    text = await execute_tool(name, arguments, context)
    return _result(
        request_id,
        {"content": [{"type": "text", "text": text}], "isError": _is_error_result(text)},
    )


def _is_error_result(text: str) -> bool:
    if text.startswith("Error"):
        return True
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and ("error" in data or data.get("success") is False)


async def _handle_message(request: Request, grant: AgentToolGrant, message) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Invalid JSON-RPC message")
    method = message.get("method")
    if not isinstance(method, str):
        # A response to a server request; this server never sends any.
        return None
    if "id" not in message:
        # Notifications (notifications/initialized, cancellations) need no reply.
        return None

    request_id = message["id"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return _result(request_id, _initialize_result(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, await _tools_list(grant))
    if method == "tools/call":
        return await _tools_call(request, grant, request_id, params)
    return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


@router.post("/agent")
async def agent_mcp(request: Request):
    client_host = request.client.host if request.client else ""
    if client_host not in LOOPBACK_HOSTS and client_host != cptr_bind_host():
        return JSONResponse({"error": "forbidden"}, 403)
    grant = resolve_grant(_bearer_token(request))
    if grant is None:
        return JSONResponse({"error": "unauthorized"}, 401)

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), 400)

    if isinstance(payload, list):
        replies = [reply for m in payload if (reply := await _handle_message(request, grant, m))]
        return JSONResponse(replies) if replies else Response(status_code=202)

    reply = await _handle_message(request, grant, payload)
    return JSONResponse(reply) if reply else Response(status_code=202)


@router.get("/agent")
async def agent_mcp_stream():
    # No server-to-client stream: everything is answered inline.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/agent")
async def agent_mcp_end_session():
    return Response(status_code=405, headers={"Allow": "POST"})
