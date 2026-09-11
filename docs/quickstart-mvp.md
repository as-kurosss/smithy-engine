# MVP quickstart: designer → orchestrator → agent

This is the shortest path to a **showable** SmithCore setup: edit a Windows flow
in the designer, publish it to the orchestrator as a pack, and run it on a
Windows agent. No Python in the flow and no runner shim — packs ship *flows*,
the engine executes them against its registered tools.

Read this together with each repo's README for full setup:

| Repo | Role | Open? |
| --- | --- | --- |
| `smithcore-engine` | flow runtime, tools, pack build | MIT |
| `smithcore-designer` | visual editor + step debugger | MIT |
| `smithcore-agent` | Windows worker that runs packs | MIT |
| `smithcore-cloud` | orchestrator (registry, runs, queues, RBAC, license) | proprietary |

## The two-tab workflow

```
[ Designer tab ]  edit + debug flow  ──Publish──▶  [ Orchestrator tab ]
                                                     deploy to agent, run,
                                                     watch live logs
```

The designer and the orchestrator run in two browser tabs (or two screens).
You edit and step through the flow in the designer; the moment it looks right
you hit **Publish**, switch tabs, deploy and run.

## 1. Engine + Windows tools

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install "smithcore-engine[windows]"
```

Notepad (used by the demo pack) is on the default process allowlist; extend it
with `SMITHCORE_ALLOWED_COMMANDS` when a flow starts other apps.

## 2. Orchestrator

Follow `smithcore-cloud/README.md` (Docker for PostgreSQL, `alembic upgrade head`,
`uvicorn smithcore_cloud.main:app`). Register a user, then create an API token in
the web UI: **avatar → API tokens**.

## 3. Agent (on the Windows machine that has the desktop)

```powershell
irm https://raw.githubusercontent.com/as-kurosss/smithcore-agent/master/install-agent.ps1 | iex
```

The agent installs as a scheduled task inside the automation user's log-on
session — UI automation needs an interactive desktop.

## 4. Designer

```powershell
pip install smithcore-designer
cd designer-web; npm install; npm run build; cd ..
smithcore-designer flow.json          # opens http://127.0.0.1:8756
```

Drag `windows.*` tools onto the canvas (the palette is read from the engine's
tool registry), or open `smithcore-engine/examples/packs/notepad/flow.json`
as a starting point. Use **Debug** to step through it on the real desktop.

## 5. Publish + run

In the designer click **Publish**, enter the orchestrator URL and API token,
give the pack a name and version, and confirm. The cloud materializes a process
with that name; **Open in Orchestrator** jumps straight to it.

From the CLI:

```powershell
$env:SMITHCORE_API_TOKEN = "sct_..."
smithcore-pack push examples\packs\notepad --name notepad-demo --version 1.0.0 `
    --api-url http://your-orchestrator:8000/api
```

Then in the orchestrator: **deploy** the process to the agent and **run** it;
live logs stream over the websocket.

## Verify a pack without the orchestrator

```powershell
smithcore-run-flow --pack examples\packs\notepad --stage process   # executes
smithcore-run-flow examples\packs\notepad\flow.json --validate
```

## Current limits (Community tier)

With no license file the orchestrator enforces Community limits (1 agent,
2 processes, 2 triggers); a signed license raises them. The product stays
fully functional inside the limits — licensing only degrades to the free tier.
