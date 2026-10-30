"""Codex app-server adapter."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
from collections import deque
from contextlib import suppress
from typing import Any, AsyncIterator

from cptr.utils.agents.attachments import PreparedAgentAttachments
from cptr.utils.agents.cptr_mcp import MCP_SERVER_NAME, TOKEN_ENV_VAR, CptrMcpSpec
from cptr.utils.agents.events import (
    AgentDone,
    AgentError,
    AgentEvent,
    AgentReasoningDelta,
    AgentTextDelta,
    AgentToolOutputDelta,
    AgentToolUpdate,
)
from cptr.utils.agents.prompts import session_turn_prompt_text
from cptr.utils.identity import env_for, preexec_for

CODEX_STDOUT_CHUNK_SIZE = 64 * 1024
CODEX_MAX_WIRE_MESSAGE_CHARS = 16 * 1024 * 1024
# Codex ends a turn on `serverOverloaded` ("Selected model is at capacity") and does not retry it
# (`willRetry: false`); its own retries cover only dropped connections and streams. These are the
# waits before the turns cptr starts again, one per retry.
CODEX_OVERLOAD_RETRY_DELAYS = (5.0, 15.0, 45.0)


class CodexAppServer:
    def __init__(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        preexec_fn=None,
        config_args: list[str] | None = None,
    ) -> None:
        self.command = command
        self.config_args = config_args or []
        self.cwd = cwd
        self.env = env
        self.preexec_fn = preexec_fn
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.approval_mode = "auto"
        self.next_id = 1

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.command,
            "app-server",
            *self.config_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd or os.getcwd(),
            env=self.env,
            preexec_fn=self.preexec_fn,
        )
        self.reader_task = asyncio.create_task(self._reader_loop())
        self.stderr_task = asyncio.create_task(self._stderr_loop())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "cptr", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized")

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        for task in (self.reader_task, self.stderr_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[request_id] = future
        await self._send({"id": request_id, "method": method, "params": params})
        response = await future
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        return response

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        await self._send({"id": request_id, "result": result})

    async def respond_error(self, request_id: str | int, code: int, message: str) -> None:
        await self._send({"id": request_id, "error": {"code": code, "message": message}})

    async def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        reader_error: str | None = None
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = await self.proc.stdout.read(CODEX_STDOUT_CHUNK_SIZE)
                if not chunk:
                    break
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    await self._handle_stdout_line(line.rstrip("\r"))
                if len(buffer) > CODEX_MAX_WIRE_MESSAGE_CHARS:
                    raise RuntimeError("Codex app-server emitted an oversized message")
            buffer += decoder.decode(b"", final=True)
            if buffer.strip():
                await self._handle_stdout_line(buffer.rstrip("\r"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced in the chat.
            reader_error = str(exc)
        finally:
            if reader_error:
                message = f"Codex app-server transport error: {reader_error}"
                self._fail_pending(message)
                await self.events.put({"method": "transport/error", "params": {"message": message}})
            else:
                message = self._exit_message()
                self._fail_pending(message)
                await self.events.put({"method": "process/exited", "params": {"message": message}})

    async def _handle_stdout_line(self, line: str) -> None:
        if not line.strip():
            return
        if len(line) > CODEX_MAX_WIRE_MESSAGE_CHARS:
            raise RuntimeError("Codex app-server emitted an oversized message")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("Codex app-server emitted an invalid message")
        await self._route_message(message)

    async def _route_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            await self._handle_server_request(message)
            return
        if (
            "id" in message
            and "method" not in message
            and ("result" in message or "error" in message)
        ):
            try:
                request_id = int(message["id"])
            except (TypeError, ValueError):
                return
            future = self.pending.pop(request_id, None)
            if future and not future.done():
                future.set_result(message)
            return
        if "method" in message:
            await self.events.put(message)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        method = str(message.get("method") or "")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            decision = "decline" if self.approval_mode == "ask" else "accept"
            await self.respond(request_id, {"decision": decision})
            return
        if method in {"applyPatchApproval", "execCommandApproval"}:
            decision = "denied" if self.approval_mode == "ask" else "approved"
            await self.respond(request_id, {"decision": decision})
            return
        params = message.get("params")
        if (
            method == "mcpServer/elicitation/request"
            and isinstance(params, dict)
            and params.get("serverName") == MCP_SERVER_NAME
        ):
            # cptr's server never elicits; this is Codex asking to approve a cptr tool call.
            # It follows the same rule as commands and file changes above.
            if self.approval_mode == "ask":
                await self.respond(request_id, {"action": "decline"})
            else:
                await self.respond(request_id, {"action": "accept", "content": {}})
            return
        await self.respond_error(request_id, -32601, f"Method not found: {method}")

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            try:
                line = await self.proc.stderr.readline()
            except ValueError as exc:
                self._append_stderr(f"stderr line too long: {exc}", truncated=True)
                continue
            if not line:
                break
            self._append_stderr(line.decode(errors="replace"))

    def _append_stderr(self, text: str, *, truncated: bool = False) -> None:
        text = text.strip()
        if text:
            suffix = " [truncated]" if truncated else ""
            self.stderr_tail.append(f"{text[:2000]}{suffix}")

    def _fail_pending(self, message: str) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError(message))
        self.pending.clear()

    def _exit_message(self) -> str:
        detail = "\n".join(self.stderr_tail).strip()
        if detail:
            return f"Codex app-server exited: {detail}"
        if self.proc and self.proc.returncode is not None:
            return f"Codex app-server exited with code {self.proc.returncode}."
        return "Codex app-server exited."


def _approval_policy(value: str) -> str:
    return {"ask": "on-request", "auto": "on-request", "full": "never"}.get(value, "on-request")


def _chat_approval_mode(chat_params: dict[str, Any]) -> str:
    if "tool_approval_mode" in chat_params:
        return str(chat_params.get("tool_approval_mode") or "auto")
    if "auto_approve_tools" in chat_params:
        return "full" if chat_params.get("auto_approve_tools") else "auto"
    return "auto"


def _sandbox_for_approval(value: str) -> str:
    if value == "ask":
        return "read-only"
    if value == "full":
        # Codex docs: full access is danger-full-access together with approval policy never.
        return "danger-full-access"
    return "workspace-write"


def _codex_turn_options(chat_params: dict[str, Any]) -> dict[str, str]:
    options: dict[str, str] = {}
    effort = chat_params.get("reasoningEffort") or chat_params.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        options["effort"] = effort.strip()
    service_tier = chat_params.get("serviceTier") or chat_params.get("service_tier")
    if isinstance(service_tier, str) and service_tier.strip():
        options["serviceTier"] = service_tier.strip()
    return options


def _event_turn_id(params: dict[str, Any]) -> str | None:
    """The turn a notification belongs to: `turnId`, or `turn.id` on `turn/started|completed`."""
    value = params.get("turnId")
    if not value and isinstance(params.get("turn"), dict):
        value = params["turn"].get("id")
    return value if isinstance(value, str) and value else None


def _codex_error_info(error: Any) -> str | None:
    """`codexErrorInfo` as its name: a plain string, or the one key of an object variant."""
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return str(next(iter(info)))
    return None


def _codex_error_message(error: Any) -> str | None:
    """Words for a Codex `TurnError`, not the raw payload."""
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    details = str(error.get("additionalDetails") or "").strip()
    if message and details and details not in message:
        return f"{message}\n{details}"
    return message or details or None


def _resume_thread_id(resume_state: dict[str, Any] | None) -> str | None:
    if not resume_state:
        return None
    value = resume_state.get("thread_id")
    return value if isinstance(value, str) and value.strip() else None


async def _open_thread(
    client: CodexAppServer,
    thread_params: dict[str, Any],
    resume_state: dict[str, Any] | None,
) -> str | None:
    thread_id = _resume_thread_id(resume_state)
    if thread_id:
        try:
            response = await client.request(
                "thread/resume", {"threadId": thread_id, **thread_params}
            )
        except RuntimeError:
            response = await client.request("thread/start", thread_params)
    else:
        response = await client.request("thread/start", thread_params)

    thread = response.get("result", {}).get("thread", {})
    value = thread.get("id") if isinstance(thread, dict) else None
    return value if isinstance(value, str) and value else None


def _tool_from_item_event(method: str, params: dict[str, Any]) -> AgentToolUpdate | None:
    if method not in {"item/started", "item/completed"}:
        return None
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = str(item.get("type") or "").strip()
    normalized_type = re.sub(r"[^a-z0-9]+", " ", item_type.lower()).strip()
    if not _is_tool_item_type(normalized_type):
        return None
    call_id = params.get("itemId") or item.get("id") or item.get("callId")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    if item_type == "mcpToolCall":
        return _mcp_tool_update(method, call_id.strip(), item)
    title = str(item.get("title") or item.get("name") or item_type or "Codex action").strip()
    detail = _item_detail(item)
    return AgentToolUpdate(
        call_id=call_id.strip(),
        name="run_command" if "command" in normalized_type and detail else "agent_tool",
        status="completed" if method == "item/completed" else "in_progress",
        arguments={"command": detail}
        if "command" in normalized_type and detail
        else {"title": title},
        output=None,
    )


def _mcp_tool_update(method: str, call_id: str, item: dict[str, Any]) -> AgentToolUpdate:
    """An MCP call with its server, tool, arguments and result, so the chat shows what ran."""
    server = str(item.get("server") or "mcp")
    tool = str(item.get("tool") or "tool")
    raw_arguments = item.get("arguments")
    arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
    arguments["title"] = f"{server}.{tool}"
    output = None
    if method == "item/completed":
        error = item.get("error")
        result = item.get("result")
        if isinstance(error, dict) and error.get("message"):
            output = f"Error: {error['message']}"
        elif isinstance(result, dict):
            texts = [
                block.get("text", "")
                for block in result.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            output = "\n".join(text for text in texts if text) or None
    return AgentToolUpdate(
        call_id=call_id,
        name="agent_tool",
        status="completed" if method == "item/completed" else "in_progress",
        arguments=arguments,
        output=output,
    )


def _is_tool_item_type(normalized_type: str) -> bool:
    if not normalized_type:
        return False
    blocked = ("user", "agent message", "assistant", "reasoning", "thought", "plan", "todo")
    if any(value in normalized_type for value in blocked):
        return False
    allowed = (
        "command",
        "file change",
        "patch",
        "edit",
        "mcp",
        "dynamic tool",
        "collab",
        "web search",
        "image",
        "error",
    )
    return any(value in normalized_type for value in allowed)


def _item_detail(item: dict[str, Any]) -> str | None:
    for key in ("command", "title", "summary", "text", "path", "prompt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _tool_output_from_event(method: str, params: dict[str, Any]) -> AgentToolOutputDelta | None:
    if method not in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
        return None
    call_id = params.get("itemId")
    delta = params.get("delta")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    if not isinstance(delta, str) or not delta:
        return None
    return AgentToolOutputDelta(
        call_id=call_id.strip(),
        delta=delta,
        stream_kind="command_output"
        if method == "item/commandExecution/outputDelta"
        else "file_change_output",
    )


async def run_codex_agent(
    *,
    profile: dict[str, Any],
    model: str,
    workspace: str,
    messages: list[dict[str, Any]],
    system_prompt: str,
    chat_params: dict[str, Any],
    resume_state: dict[str, Any] | None,
    attachments: PreparedAgentAttachments,
    identity=None,
    cptr_mcp: CptrMcpSpec | None = None,
) -> AsyncIterator[AgentEvent]:
    env = env_for(identity, workspace) if identity and identity.is_pam else os.environ.copy()
    # Codex reports this as the client in its usage statistics. Only Codex gets it: other agents
    # would rather not be told they are driven by OpenClaw.
    env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = "openclaw"
    if profile.get("home"):
        env["CODEX_HOME"] = os.path.expanduser(str(profile["home"]))
    config_args: list[str] = []
    if cptr_mcp:
        # The token goes through the environment so it never shows up in `ps`.
        env[TOKEN_ENV_VAR] = cptr_mcp.token
        config_args = cptr_mcp.codex_config_args(
            auto_approve=_chat_approval_mode(chat_params) != "ask"
        )

    client = CodexAppServer(
        str(profile["command"]),
        workspace,
        env,
        preexec_for(identity) if identity and identity.is_pam else None,
        config_args,
    )
    thread_id: str | None = None
    turn_id: str | None = None
    try:
        approval_mode = _chat_approval_mode(chat_params)
        client.approval_mode = approval_mode
        await client.start()
        thread_params: dict[str, Any] = {
            "cwd": workspace,
            "approvalPolicy": _approval_policy(approval_mode),
            "sandbox": _sandbox_for_approval(approval_mode),
        }
        if model != "default":
            thread_params["model"] = model
        if system_prompt:
            thread_params["developerInstructions"] = system_prompt

        thread_id = await _open_thread(client, thread_params, resume_state)
        if not thread_id:
            yield AgentError("Codex did not return a thread id")
            return

        prompt = session_turn_prompt_text(messages, resumed=bool(_resume_thread_id(resume_state)))
        turn_input: list[dict[str, Any]] = []
        if prompt:
            turn_input.append({"type": "text", "text": prompt})
        for image in attachments.images:
            turn_input.append(
                {
                    "type": "image",
                    "url": f"data:{image.mime_type};base64,{image.base64}",
                }
            )
        overload_retries = 0
        while True:
            turn_response = await client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": turn_input,
                    **_codex_turn_options(chat_params),
                },
            )
            turn = turn_response.get("result", {}).get("turn", {})
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            # Text or tool activity already shown for this turn: running it again would repeat it.
            progressed = False
            # The notice of a retry Codex is making on its own, shown until the turn moves on.
            reconnecting: AgentToolUpdate | None = None

            while True:
                event = await client.events.get()
                method = event.get("method")
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                event_turn_id = _event_turn_id(params)
                if event_turn_id and turn_id and event_turn_id != turn_id:
                    continue
                if reconnecting and method != "error":
                    yield AgentToolUpdate(
                        call_id=reconnecting.call_id,
                        name=reconnecting.name,
                        status="completed",
                        arguments=reconnecting.arguments,
                    )
                    reconnecting = None
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        progressed = True
                        yield AgentTextDelta(delta)
                elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        yield AgentReasoningDelta(delta)
                else:
                    tool_output = _tool_output_from_event(str(method or ""), params)
                    if tool_output:
                        progressed = True
                        yield tool_output
                    else:
                        tool = _tool_from_item_event(str(method or ""), params)
                        if tool:
                            progressed = True
                            yield tool
                if method == "turn/completed":
                    completed_turn = params.get("turn")
                    status = (
                        completed_turn.get("status") if isinstance(completed_turn, dict) else None
                    )
                    if status in ("failed", "interrupted"):
                        error = completed_turn.get("error")
                        yield AgentError(_codex_error_message(error) or f"Codex turn {status}")
                        return
                    yield AgentDone(
                        resume_state={
                            "profile_id": profile["id"],
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "workspace": workspace,
                            "model": model,
                        }
                    )
                    return
                elif method == "error":
                    error = params.get("error")
                    message = _codex_error_message(error)
                    if params.get("willRetry"):
                        # Codex is retrying this itself ("Reconnecting... 2/5"): the turn is
                        # still alive, so only show where it is and keep waiting.
                        title = (message or "Codex is reconnecting").splitlines()[0][:200]
                        reconnecting = AgentToolUpdate(
                            call_id="codex-reconnecting",
                            name="agent_tool",
                            status="in_progress",
                            arguments={"title": title},
                        )
                        yield reconnecting
                        continue
                    if (
                        _codex_error_info(error) == "serverOverloaded"
                        and not progressed
                        and overload_retries < len(CODEX_OVERLOAD_RETRY_DELAYS)
                    ):
                        # Codex ends the turn on a capacity error and does not retry it. With
                        # nothing shown or run yet, a new turn repeats nothing.
                        delay = CODEX_OVERLOAD_RETRY_DELAYS[overload_retries]
                        overload_retries += 1
                        notice = AgentToolUpdate(
                            call_id=f"codex-capacity-{overload_retries}",
                            name="agent_tool",
                            status="in_progress",
                            arguments={
                                "title": "Codex model at capacity, retrying in "
                                f"{delay:g}s ({overload_retries}/{len(CODEX_OVERLOAD_RETRY_DELAYS)})"
                            },
                        )
                        yield notice
                        await asyncio.sleep(delay)
                        yield AgentToolUpdate(
                            call_id=notice.call_id,
                            name=notice.name,
                            status="completed",
                            arguments=notice.arguments,
                        )
                        break
                    yield AgentError(message or str(params or "Codex app-server error"))
                    return
                elif method == "process/exited":
                    yield AgentError(str(params.get("message") or "Codex app-server exited."))
                    return
                elif method == "transport/error":
                    yield AgentError(
                        str(params.get("message") or "Codex app-server transport error")
                    )
                    return
            # Only a capacity retry leaves the loop above (every other end returns), so the next
            # pass starts the turn again.
    except asyncio.CancelledError:
        if thread_id and turn_id:
            with suppress(Exception):
                await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced in the chat.
        yield AgentError(str(exc))
    finally:
        await client.close()
