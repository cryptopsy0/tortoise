# tortoise

Standalone Python terminal UI for Hermes.

What it does
 runs a curses full-screen chat UI
 calls the installed `hermes --oneshot` command for each prompt
 keeps a local transcript in `~/.local/state/tortoise/transcript.jsonl`
 includes recent transcript text in each prompt so conversation continues
 needs only Python stdlib plus the installed `hermes` CLI

Run
```bash
~/tortoise/tortoise
```

Optional
```bash
~/tortoise/tortoise --model gpt-5.5
~/tortoise/tortoise --provider openai-codex
~/tortoise/tortoise --toolsets terminal,file,web
~/tortoise/tortoise --transcript ~/tortoise/transcript.jsonl
```

Commands inside the UI
 `/help`
  show commands
 `/new` or `/reset`
  clear transcript
 `/clear`
  clear screen state
 `/model [name]`
  show or set model override
 `/provider [name]`
  show or set provider override
 `/tools [list]`
  show or set toolsets override
 `/quit` `/exit` `/q`
  exit

Notes
 this is standalone and update-safe
 it does not patch Hermes Agent source
 it is not the Node/Ink `hermes --tui` frontend
 it wraps Hermes via the CLI, so exact live streaming and native Hermes TUI widgets are intentionally simplified
