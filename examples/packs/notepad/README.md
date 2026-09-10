# Notepad demo pack

A minimal, end-to-end Windows flow **and a template**: launch Notepad, focus
its window by PID, type `$text`, capture a screenshot, close the process.

| Node | Tool | What it does |
| --- | --- | --- |
| `launch` | `windows.process` | starts `notepad.exe`, stores `{pid}` in `$app` |
| `settle` | `windows.delay` | waits 1.5 s for the window to appear |
| `focus` | `windows.window` | activates the window by `$app.pid` |
| `type` | `windows.input_text` | types `$text` (SendInput Unicode) |
| `shot` | `windows.screenshot` | saves `notepad-demo.png` next to the pack |
| `close` | `windows.process` | stops the process by PID |

`$app.pid` is an integer-preserving reference: a config value that is exactly
one `$ref` keeps the referenced type, so the `pid` field stays an integer.

## Files

```
flow.json       the main flow (a pack's main stage; "process" internally)
template.json   the template descriptor (title, params) — see below
```

## It is a template

`template.json` advertises the input the pack needs, so a catalog can prompt
for it and instantiate the pack per client:

```json
{
  "schema": "smithy-template-v1",
  "title": "Notepad demo",
  "params": [{ "name": "text", "type": "string", "default": "Hello from Smithy!" }]
}
```

`build_pack` validates it and embeds a summary under the manifest's `template`
key, so the catalog lists templates without unzipping. Parameter types:
`string / number / integer / bool / file / folder / asset / choice` (`asset`
binds to a client secret at instantiation time).

## Run it locally (no orchestrator)

```powershell
# from the repo root, in a venv with the windows extras:
smithy-run-flow --pack examples\packs\notepad --stage process --set text="Hello from Smithy!"
# or validate without executing:
smithy-run-flow examples\packs\notepad\flow.json --validate
```

Notepad must be on the process allowlist — `notepad.exe` is in the built-in
demo list, or set `SMITHY_ALLOWED_COMMANDS=notepad.exe,...`.

## Publish it to smithy-cloud

```powershell
$env:SMITHY_API_TOKEN = "sct_..."
smithy-pack push examples\packs\notepad --name notepad-demo --version 1.0.0 `
    --api-url http://your-orchestrator:8000/api
```

Or open `flow.json` in the designer and use **Publish**.
