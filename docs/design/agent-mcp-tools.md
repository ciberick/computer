# Tools de cptr para agentes externos vía MCP

Estado: fases 1 y 2 implementadas · Fork ciberick/computer · Actualizado: 2026-10-15

## 0b. Modelos de automatización y fase 2 (Cursor)

**Modelos (`feat/automation-models`).** Codex guardó `agent:grok4.7` en lugar de
`agent:cursor/grok-4.7`: `create_automation` no aceptaba modelo, `update_automation` aceptaba
cualquier texto y nada listaba los IDs válidos. Ahora:
- Tool `list_models` (grupo automatizaciones) con la misma lista que `GET /api/models`
  (`routers/chat.py:list_available_models`).
- `model_id` opcional en `create_automation`; create y update lo validan y, si no existe,
  responden con sugerencias ("Did you mean: agent:cursor/grok-4.7?").
- update/toggle/delete solo actúan sobre automatizaciones del propio usuario.
- `create_automation` rechaza un `user_id` que no existe (cierra el atajo de importar cptr con
  un usuario inventado, visto en el chat `2f4bba7b`).
- Codex muestra servidor, tool, argumentos y resultado de cada llamada MCP.

**Fase 2 (`feat/agent-mcp-cursor`).**
- Cursor anuncia `mcpCapabilities.http: true` (verificado con `initialize`), así que recibe el
  servidor `cptr` por HTTP en `session/new` y `session/load`. El puente stdio no hizo falta.
- Cursor pide permiso para cada tool MCP con el título `cptr-<tool>: <tool>`, y cptr en modo
  auto cancela todos los permisos de Cursor. Se aprueba (`allow_once`) solo cuando el título
  coincide exactamente con una tool concedida y el `kind` no es un comando ni una edición, en
  modo auto o full; en "preguntar" se sigue cancelando.
- Agentes sin MCP todavía (OpenCode, Gemini, Cline, Grok, Pi): con el interruptor activo reciben
  un bloque `<cptr_tools>` que dice que esas funciones no están disponibles y que no las imiten.
- Probado con el runner real de Cursor: la petición original ("usar el modelo agent grok4.7")
  terminó en `list_models` + `create_automation` con `agent:cursor/grok-4.7`.

Pendiente (fase 3): OpenCode, Gemini, Cline y Grok, sin CLI local para probarlos; puente stdio
solo si alguno no admite MCP por HTTP; servidores de "Servidores de herramientas" vía el mismo
endpoint.

## 0. Fase 1: lo implementado y en qué cambió respecto al diseño

Implementado: endpoint `POST /api/mcp/agent`, credenciales, claves de config, UI en General y
la conexión con Claude Code y Codex. Se probó con las CLI reales (Claude Code por
`claude-agent-sdk` 0.2.167 y `codex app-server` 0.162.1): ambas listan y llaman las tools, y la automatización queda con el
`user_id` y el `model_id` de la credencial.

Cambios respecto a lo que sigue más abajo:

- **Token por chat, no por turno (§3.2).** cptr mantiene vivo el proceso de Claude Code entre
  turnos (`_claude_clients`); un token por turno lo dejaba sin acceso desde el segundo turno. El
  token se ata a (usuario, chat, workspace, modelo), se reutiliza mientras esa combinación no
  cambie y caduca tras 6 h sin uso. Las tools permitidas **se recalculan en cada llamada** con la
  config actual: apagar el interruptor corta el acceso al instante, también a procesos ya abiertos.
- **Reglas de las tools:** además de `memory.enabled` y del chat sin workspace, se respetan los
  grupos por modelo de `chat.models[...].params.builtin_tools` (la misma fuente que usan los modelos
  de API), no `tool_approval.builtin_tools`.
- **Aprobaciones (§5):** las tools de cptr siguen la misma regla que cada adaptador ya aplica a las
  tools propias del agente. En modo auto o full se pre-aprueban (Claude Code: `allowed_tools`
  `mcp__cptr__*`; Codex: `default_tools_approval_mode="approve"` y aceptar
  `mcpServer/elicitation/request` del servidor `cptr`). En modo "preguntar" no, porque cptr aún no
  puede mostrar una aprobación de un agente externo.
- **Host:** `cli.py` exporta `CPTR_HOST`; si cptr escucha en una IP concreta se usa esa IP en lugar
  de 127.0.0.1, y el endpoint la acepta además de loopback.
- **i18n:** solo `en` y `es`; los demás idiomas usan el inglés de respaldo, como ya pasa con
  ~60 claves. Los nombres de los grupos reutilizan `models.builtinTools.*`.
- **Codex `-c`:** verificado, `codex app-server` acepta los overrides.

## 1. Problema

Los modelos `agent:*` (Cursor, Claude Code, Codex, OpenCode, Gemini, Cline, Grok) corren su propia
CLI y solo ven sus tools nativas. cptr no les pasa ninguna de las suyas:

- `cptr/utils/agents/acp.py:178` y `:183` abren la sesión ACP con `"mcpServers": []`.
- `claude_code.py`, `codex.py` y `opencode.py` no configuran ningún servidor MCP
  (`opencode.py` incluso fuerza `OPENCODE_CONFIG_CONTENT="{}"`).

Resultado observado (chat `d9542ce1`): el agente no sabía que existía `create_automation`, armó
bucles `sleep`/cron y, cuando se le nombró la tool, la invocó importando `cptr.utils.tools` en un
proceso Python aparte, adivinando `user_id` y `model_id` y saltándose las aprobaciones.

## 2. Objetivo y alcance

Exponer a los agentes externos, mediante un servidor MCP servido por el propio cptr, **solo** estas
tools ya existentes:

| Grupo (`BUILTIN_TOOL_GROUPS`) | Tools |
|---|---|
| `automations` | `list_automations`, `create_automation`, `update_automation`, `toggle_automation`, `delete_automation` |
| `notifications` | `notify` |
| `memory` | `update_memory` |

Configurable desde **Configuración general**, con el mismo mecanismo de configuración del servidor
que usan las demás opciones de admin.

Fuera de alcance: archivos, terminal, navegador, imágenes, sub-agentes (los agentes ya tienen sus
equivalentes) y Pi (su modo RPC no admite MCP).

## 3. Arquitectura

```
 cptr (uvicorn, 127.0.0.1:CPTR_PORT)
 ├─ chat_task._run_agent_target
 │    1. si agents.cptr_tools.enabled → AgentToolGrant.issue(ctx)  ──► token opaco
 │    2. runner(..., cptr_mcp=CptrMcpSpec(url, token, tools))
 │    3. finally: AgentToolGrant.revoke(token)
 │
 ├─ POST /api/mcp/agent   (router nuevo, JSON-RPC MCP "Streamable HTTP")
 │    initialize · tools/list · tools/call · ping
 │    token → ctx {user_id, workspace, model_id, chat_id, tools}
 │    tools/call → tools.execute_tool(name, args, ctx)   ← misma ruta que los modelos de API
 │
 └─ cptr mcp-stdio        (subcomando nuevo: puente stdio→HTTP, solo para agentes ACP sin HTTP)

 CLI del agente (proceso hijo)
   Claude Code ── mcp_servers={"cptr": {type: http, url, headers}}
   Codex       ── -c mcp_servers.cptr.url=… + bearer_token_env_var=CPTR_MCP_TOKEN
   OpenCode    ── OPENCODE_CONFIG_CONTENT={"mcp":{"cptr":{type: remote, url, headers}}}
   ACP (Cursor, Gemini, Cline, Grok)
               ── session/new|load mcpServers=[http si mcpCapabilities.http, si no stdio → cptr mcp-stdio]
```

### 3.1 Endpoint MCP (`cptr/routers/agent_mcp.py`)

- Implementación JSON-RPC propia y mínima, **sin depender del paquete `mcp`**: es un extra opcional
  y `install.sh` solo instala `--extra agents`. Streamable HTTP permite que el servidor responda
  `application/json` a cada POST, sin SSE ni sesión. Hacen falta cuatro métodos: `initialize`
  (devuelve `protocolVersion`, `capabilities.tools`, `serverInfo: {name: "cptr"}`), `tools/list`,
  `tools/call` y `ping`. Las notificaciones (`notifications/initialized`) responden `202`.
- `GET /api/mcp/agent` → `405` (sin stream servidor→cliente).
- `tools/list` reutiliza `tools._fn_to_schema(name, fn)` y filtra por la lista de tools del token.
  Así el esquema que ve el agente es el mismo que ve un modelo de API.
- `tools/call` llama a `tools.execute_tool(name, args, ctx)`. Con eso se heredan las reglas que ya
  existen: chat sin workspace ⇒ automations deshabilitadas (`GLOBAL_CHAT_DISABLED_TOOLS`), grupo
  apagado en `tool_approval.builtin_tools` ⇒ `tool disabled`. La respuesta va en
  `{"content":[{"type":"text","text": result}], "isError": result.startswith("Error")}`.
- `ctx` sale **solo** del token: `{"workspace", "user_id", "model_id", "chat_id", "builtin_tools",
  "request"}`. El agente nunca aporta `user_id` ni `model_id`.
- Se escribe un registro de auditoría por llamada con `utils/audit.py` (tool, user, chat, agente).

### 3.2 Credencial por turno (`cptr/utils/agent_tool_grant.py`)

- Token opaco: `secrets.token_urlsafe(32)`, guardado en un dict en memoria
  `token → GrantContext(user_id, workspace, model_id, chat_id, tools, expires_at)`. cptr es un
  solo proceso, y el token muere con el turno (`revoke` en el `finally` de `_run_agent_target`).
  Tiene además un TTL de respaldo de 6 h.
- Se usa un token opaco y no un JWT para que sea revocable al instante y para que no se pueda usar
  nunca como cookie de sesión (`check_access` no lo conoce).
- `tools` = grupos habilitados en `agents.cptr_tools.groups`, menos los apagados en
  `tool_approval.builtin_tools`, menos `update_memory` si `memory.enabled` es falso.
  Si queda vacía, no se emite token y no se inyecta nada.

### 3.3 Autenticación del endpoint

- `app.py:auth_middleware` deja pasar `path == "/api/mcp/agent"` (igual que `/api/health`). El
  router valida `Authorization: Bearer <token>` contra el registro y responde `401` si no existe o
  expiró.
- Defensa adicional: solo acepta `request.client.host` de loopback (`127.0.0.1`, `::1`). Los
  agentes siempre corren en la misma máquina que cptr.
- La URL que se inyecta es `http://127.0.0.1:{CPTR_PORT}/api/mcp/agent` (`CPTR_PORT` ya lo
  publica `cli.py:30`), nunca la URL pública.

### 3.4 Inyección por adaptador

Nuevo dataclass `CptrMcpSpec(url, token, tools)` en `cptr/utils/agents/cptr_mcp.py`, con helpers
`for_claude_code()`, `for_codex_args()`, `for_opencode()` y `for_acp(caps)`. Cada runner recibe
`cptr_mcp: CptrMcpSpec | None = None` (con `None` se comporta igual que hoy).

| Agente | Dónde | Forma |
|---|---|---|
| Claude Code | `claude_code.py:234` `options_kwargs` | `mcp_servers={"cptr": {"type":"http","url":…,"headers":{"Authorization":"Bearer …"}}}`; además `allowed_tools` += `mcp__cptr__<tool>` cuando el chat está en modo auto, para que no lo frene el permiso de la CLI |
| Codex | `codex.py:47` lanzamiento de `app-server` | `-c mcp_servers.cptr.url="…"` y `-c mcp_servers.cptr.bearer_token_env_var="CPTR_MCP_TOKEN"`; token en `env` (no en argv, para que no aparezca en `ps`) |
| OpenCode | `opencode.py:98` | `OPENCODE_CONFIG_CONTENT=json.dumps({"mcp":{"cptr":{"type":"remote","url":…,"headers":{…}}}})` |
| Cursor, Gemini, Cline, Grok | `acp.py:_open_session` (`session/new` y `session/load`) | si `initialize.agentCapabilities.mcpCapabilities.http` → `{"type":"http","name":"cptr","url":…,"headers":[{"name":"Authorization","value":"Bearer …"}]}`; si no → stdio `{"name":"cptr","command": sys.executable,"args":["-m","cptr.cli","mcp-stdio"],"env":[{"name":"CPTR_MCP_URL",…},{"name":"CPTR_MCP_TOKEN",…}]}` |

`AcpClient` recibe `mcp_servers: list[dict]` en el constructor y lo usa en lugar del `[]` fijo.

### 3.5 Puente stdio (`cptr mcp-stdio`)

Subcomando en `cli.py`: lee mensajes JSON-RPC línea a línea de stdin, los reenvía con `httpx` a
`CPTR_MCP_URL` con el bearer y escribe la respuesta en stdout. Son unas 40 líneas y no necesitan el
paquete `mcp`. Solo lo usan los agentes ACP que no anuncian MCP por HTTP.

### 3.6 Que el agente lo sepa (lo que faltó en el chat `d9542ce1`)

Cuando se inyecta el servidor, `_run_agent_target` añade al system prompt un bloque corto:

```
<cptr_tools>
cptr exposes tools to you through the MCP server "cptr": create_automation, list_automations,
update_automation, toggle_automation, delete_automation, notify, update_memory.
Use them for scheduled/recurring work, notifying the user and saving memory. Do not emulate them
with cron, sleep loops, systemd timers or by importing cptr's Python code.
</cptr_tools>
```

Solo se listan las tools concedidas en ese turno.

## 4. Configuración

### 4.1 Claves (tabla `config`, vía `PUT /api/admin/config`)

Misma convención `<área>.<opción>` que `subagents.enabled` y `memory.enabled`, y el mismo formato de
mapa de grupos que `tool_approval.builtin_tools`:

| Clave | Tipo | Default |
|---|---|---|
| `agents.cptr_tools.enabled` | bool | `false` |
| `agents.cptr_tools.groups` | `{"automations": bool, "notifications": bool, "memory": bool}` | todos `true` |

Se leen con `Config.get(...)` al inicio de cada turno; no hace falta reiniciar.

### 4.2 UI: `Settings/General.svelte`

Va dentro del bloque solo-admin que ya existe (`{#if $session?.role === 'admin'}`, debajo de
"Actualizaciones"), con los mismos estilos de `h3`, `label` + `ToggleSwitch` y `p` de ayuda:

```
Herramientas de cptr para agentes                            (h3)
  Exponer herramientas a agentes externos          [ ● ]
  Los agentes (Claude Code, Codex, Cursor…) pueden crear automatizaciones,
  enviar notificaciones y guardar memoria vía MCP.                (p)
    Automatizaciones                               [ ● ]   ← solo si el principal está activo
    Notificaciones                                 [ ● ]
    Memoria                                        [ ● ]   (deshabilitado + hint si memory.enabled = false)
```

- Carga: `getAdminConfig()` en `onMount`, parseando booleanos igual que `Subagents.svelte`
  (`=== true || === 'true'`).
- Guardado: General no tiene botón "Guardar" (idioma, cola y avisos aplican al momento), así que
  cada toggle llama `updateConfig({...})` al cambiar, con `toast.error($t('admin.failedToSave'))`
  si falla y vuelve al valor anterior.
- i18n: claves `general.agentTools`, `general.agentToolsEnabled`, `general.agentToolsDesc`,
  `general.agentTools.automations`, `general.agentTools.notifications`,
  `general.agentTools.memory`, `general.agentTools.memoryDisabled` en los 10 locales (`en` y `es`
  traducidos; el resto, copia en inglés como hacen hoy las claves nuevas).
- `normalizeTab` y `adminTabIds` no cambian: es una sección dentro de una pestaña personal, visible
  solo para admin.

## 5. Aprobaciones y seguridad

- **Permisos:** la llamada MCP pasa primero por el control de permisos del propio agente, que cptr
  ya conecta con el `tool_approval_mode` del chat (por ejemplo `cursor.py:_auto_approve`). En
  modo auto se auto-aprueba y en modo revisión el usuario ve la petición como cualquier otra tool
  del agente. No se duplica la cola de aprobación de cptr.
- **Lo que el token permite:** solo las tools concedidas, solo para el `user_id` del chat, y solo
  mientras dura el turno. Un agente no puede crear automatizaciones a nombre de otro usuario ni
  reutilizar el token después.
- **Recursión:** una automatización con modelo `agent:*` también recibe estas tools al ejecutarse.
  Es lo esperado (puede notificar), pero podría crear automatizaciones en cadena. Mitigación: en
  turnos lanzados por el scheduler (`chat.meta.automation_id` presente) se retiran `create_automation`
  y `delete_automation` del grant.
- **Datos sensibles:** el token nunca va en argv ni en logs (Codex y stdio lo reciben por env; HTTP,
  por header).

## 6. Archivos

| Archivo | Cambio |
|---|---|
| `cptr/routers/agent_mcp.py` | nuevo: endpoint JSON-RPC MCP |
| `cptr/utils/agent_tool_grant.py` | nuevo: registro de tokens y cálculo de tools concedidas |
| `cptr/utils/agents/cptr_mcp.py` | nuevo: `CptrMcpSpec` y formatos por agente |
| `cptr/app.py` | `include_router` y excepción en `auth_middleware` |
| `cptr/cli.py` | subcomando `mcp-stdio` |
| `cptr/utils/chat_task.py` | emitir y revocar el grant, pasar `cptr_mcp` al runner y bloque `<cptr_tools>` |
| `cptr/utils/agents/acp.py` | `mcp_servers` configurable y elección http/stdio según capacidades |
| `claude_code.py`, `codex.py`, `opencode.py`, `cursor.py`, `gemini.py`, `cline.py`, `grok.py` | aceptar `cptr_mcp` |
| `frontend/.../Settings/General.svelte` | sección admin |
| `frontend/src/lib/i18n/locales/*.json` | claves nuevas |

## 7. Pruebas

1. Unitarias del endpoint: `initialize`, `tools/list` filtrado por grant, `tools/call` con token
   válido, revocado, expirado o ausente (401), cliente no loopback (403), y tool fuera del grant
   (error JSON-RPC `-32602`).
2. El grant respeta `memory.enabled=false`, `tool_approval.builtin_tools.automations=false` y el
   chat global sin workspace.
3. Token opaco rechazado por `check_access` (no sirve como sesión).
4. E2E manual por agente: repetir el chat `d9542ce1` ("consulta el dólar cada 5 min") con Cursor,
   Claude Code y Codex. Esperado: la primera acción es `create_automation`, la automatización queda
   con el `user_id` y el `model_id` correctos, y no hay procesos `sleep` ni crontab.
5. Toggle principal apagado ⇒ los runners reciben `cptr_mcp=None` y el comportamiento es idéntico
   al actual.

## 8. Fases

1. Endpoint, grant y claves de config, más Claude Code y Codex (soporte HTTP nativo y robusto).
2. ACP por HTTP (Cursor, que fue el caso real) y OpenCode.
3. Puente stdio para los ACP sin HTTP y la UI en General.

En la práctica, la UI conviene meterla en la fase 1 para poder activarlo sin tocar la BD.

## 9. Por verificar antes de implementar

- Que `codex app-server` acepte los overrides globales `-c` (si no, pasarlos en `thread/start`
  `config`).
- Qué anuncia la versión de Cursor instalada en `agentCapabilities.mcpCapabilities` y si permite
  MCP en `session/load` además de `session/new`.
- Cómo gestiona hoy `claude_code.py` los permisos (`permission_mode` o `can_use_tool`), para decidir
  si hace falta `allowed_tools`.
