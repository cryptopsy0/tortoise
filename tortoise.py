#!/usr/bin/env python3
"""Standalone Python Hermes TUI.

A dependency-free curses interface that wraps the installed `hermes` CLI.
It keeps a local transcript, builds conversational context for each request,
and supports a small set of slash commands without modifying Hermes itself.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import getpass
import pwd
import queue
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


APP_NAME = "tortoise"
TRANSCRIPT_LIMIT = 12
MAX_CONTEXT_CHARS = 24_000
DEFAULT_CONFIG_TEXT = """color
 user light_gray
 assistant default
 system yellow
 header cyan
 input green
 divider blue
 context yellow
 usage magenta
 help yellow
keybindings
 quit escape
 submit enter
 scroll_up up
 scroll_down down
 page_up page_up
 page_down page_down
 backspace backspace
"""
COLOR_NAMES = {
 "black": curses.COLOR_BLACK,
 "red": curses.COLOR_RED,
 "green": curses.COLOR_GREEN,
 "yellow": curses.COLOR_YELLOW,
 "blue": curses.COLOR_BLUE,
 "magenta": curses.COLOR_MAGENTA,
 "cyan": curses.COLOR_CYAN,
 "white": curses.COLOR_WHITE,
 "light_gray": curses.COLOR_WHITE,
 "light_grey": curses.COLOR_WHITE,
 "gray": curses.COLOR_WHITE,
 "grey": curses.COLOR_WHITE,
}
KEY_NAMES = {
 "escape": (27,),
 "esc": (27,),
 "ctrl_c": (3,),
 "enter": (10, 13),
 "return": (10, 13),
 "up": (curses.KEY_UP,),
 "down": (curses.KEY_DOWN,),
 "page_up": (curses.KEY_PPAGE,),
 "page_down": (curses.KEY_NPAGE,),
 "backspace": (curses.KEY_BACKSPACE, 127, 8),
}


@dataclass
class TortoiseConfig:
 color: dict[str, str] = field(default_factory=lambda: {
  "user": "light_gray",
  "assistant": "default",
  "system": "yellow",
  "header": "cyan",
  "input": "green",
  "divider": "blue",
  "context": "yellow",
  "usage": "magenta",
  "help": "yellow",
 })
 keybindings: dict[str, str] = field(default_factory=lambda: {
  "quit": "escape",
  "submit": "enter",
  "scroll_up": "up",
  "scroll_down": "down",
  "page_up": "page_up",
  "page_down": "page_down",
  "backspace": "backspace",
 })

 @classmethod
 def load(cls, path: Path) -> "TortoiseConfig":
  path.parent.mkdir(parents=True, exist_ok=True)
  if not path.exists():
   path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
  cfg = cls()
  section = ""
  for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
   if not raw_line.strip() or raw_line.lstrip().startswith("#"):
    continue
   if raw_line[:1].isspace():
    key, _, value = raw_line.strip().partition(" ")
    if not key or not value.strip():
     continue
    if section == "color":
     cfg.color[key] = value.strip()
    if section == "keybindings":
     cfg.keybindings[key] = value.strip()
    continue
   section = raw_line.strip()
  return cfg


@dataclass
class Message:
 role: str
 text: str
 ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

 def as_dict(self) -> dict[str, str]:
  return {"role": self.role, "text": self.text, "ts": self.ts}


class Transcript:
 def __init__(self, path: Path) -> None:
  self.path = path
  self.messages: list[Message] = []
  self.load()

 def load(self) -> None:
  self.messages = []
  if not self.path.exists():
   return
  for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
   if not line.strip():
    continue
   try:
    data = json.loads(line)
    self.messages.append(Message(role=data["role"], text=data["text"], ts=data.get("ts") or ""))
   except Exception:
    continue

 def append(self, role: str, text: str) -> None:
  msg = Message(role=role, text=text)
  self.messages.append(msg)
  self.path.parent.mkdir(parents=True, exist_ok=True)
  with self.path.open("a", encoding="utf-8") as f:
   f.write(json.dumps(msg.as_dict(), ensure_ascii=False) + "\n")

 def clear(self) -> None:
  self.messages = []
  self.path.parent.mkdir(parents=True, exist_ok=True)
  self.path.write_text("", encoding="utf-8")

 def prompt_with_context(self, user_text: str) -> str:
  recent = self.messages[-TRANSCRIPT_LIMIT:]
  parts = [
   "You are continuing a local standalone Hermes TUI conversation.",
   "Use the transcript only as conversational context.",
   "Do not mention this wrapper unless asked.",
   "",
   "transcript",
  ]
  total = 0
  for msg in recent:
   item = f" {msg.role}: {msg.text.strip()}"
   total += len(item)
   if total > MAX_CONTEXT_CHARS:
    break
   parts.append(item)
  parts.extend(["", "user", f" {user_text}"])
  return "\n".join(parts)

 def context_percent(self) -> int:
  prompt = self.prompt_with_context("")
  return max(0, min(100, round((len(prompt) / MAX_CONTEXT_CHARS) * 100)))


def launch_user_home() -> Path:
 explicit_home = os.environ.get("TORTOISE_USER_HOME")
 if explicit_home:
  return Path(explicit_home).expanduser()
 sudo_user = os.environ.get("SUDO_USER")
 if sudo_user and sudo_user != "root":
  try:
   return Path(pwd.getpwnam(sudo_user).pw_dir)
  except KeyError:
   pass
 if os.geteuid() == 0 and Path("/h/.hermes").exists():
  return Path("/h")
 try:
  return Path(pwd.getpwnam(getpass.getuser()).pw_dir)
 except KeyError:
  return Path.home()


def default_hermes_bin() -> str:
 explicit_bin = os.environ.get("TORTOISE_HERMES_BIN")
 if explicit_bin:
  return explicit_bin
 user_bin = launch_user_home() / ".local" / "bin" / "hermes"
 if user_bin.exists():
  return str(user_bin)
 return shutil.which("hermes") or "hermes"


class HermesRunner:
 def __init__(self, hermes_bin: str, model: str | None, provider: str | None, toolsets: str | None) -> None:
  self.hermes_bin = hermes_bin
  self.model = model
  self.provider = provider
  self.toolsets = toolsets
  self.user_home = launch_user_home()

 def command(self, prompt: str) -> list[str]:
  cmd = [self.hermes_bin]
  if self.model:
   cmd += ["--model", self.model]
  if self.provider:
   cmd += ["--provider", self.provider]
  if self.toolsets:
   cmd += ["--toolsets", self.toolsets]
  cmd += ["--oneshot", prompt]
  return cmd

 def env(self) -> dict[str, str]:
  env = os.environ.copy()
  env.setdefault("HOME", str(self.user_home))
  if Path(env.get("HOME", "")) == Path("/root") and self.user_home != Path("/root"):
   env["HOME"] = str(self.user_home)
  env.setdefault("HERMES_HOME", str(self.user_home / ".hermes"))
  return env

 def ask(self, prompt: str) -> str:
  try:
   proc = subprocess.run(
    self.command(prompt),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=None,
    check=False,
    env=self.env(),
   )
  except FileNotFoundError:
   return "hermes executable not found. install Hermes or pass --hermes-bin."
  except KeyboardInterrupt:
   return "interrupted"
  out = proc.stdout.strip()
  err = proc.stderr.strip()
  if proc.returncode == 0:
   return out or "(empty response)"
  detail = err or out or f"exit code {proc.returncode}"
  return f"hermes failed: {detail}"


def load_usage_percent(env: dict[str, str], user_home: Path) -> int:
 script = """
import json
import sys
sys.path.insert(0, '{home}/.hermes/hermes-agent')
from tui_gateway import server
info = server._lightweight_session_info()
usage = info.get('usage') or {{}}
value = usage.get('codex_all_auth_used_percent')
if value is None:
 value = usage.get('codex_five_hour_used_percent')
print(0 if value is None else round(float(value)))
""".format(home=str(user_home))
 python_bin = user_home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
 try:
  proc = subprocess.run(
   [str(python_bin) if python_bin.exists() else sys.executable, "-c", script],
   text=True,
   stdout=subprocess.PIPE,
   stderr=subprocess.PIPE,
   timeout=3,
   check=False,
   env=env,
  )
 except Exception:
  return 0
 match = re.search(r"-?\d+", (proc.stdout + proc.stderr).strip())
 if not match:
  return 0
 return max(0, min(100, int(match.group(0))))


class TortoiseApp:
 def __init__(self, stdscr, transcript: Transcript, runner: HermesRunner, config: TortoiseConfig) -> None:
  self.stdscr = stdscr
  self.transcript = transcript
  self.runner = runner
  self.config = config
  self.color_attrs: dict[str, int] = {"default": curses.A_NORMAL}
  self.input_text = ""
  self.scroll = 0
  self.status = "ready"
  self.reply_queue: queue.Queue[tuple[str, str]] = queue.Queue()
  self.running = True
  self.busy = False
  self.busy_started = 0.0
  self.context_percent = self.transcript.context_percent()
  self.usage_percent = load_usage_percent(self.runner.env(), self.runner.user_home)
  self.help_text = (
   ":help commands | :new clear transcript | :clear clear screen | "
   ":model show model | :r restart | :quit exit"
  )

 def run(self) -> None:
  curses.curs_set(1)
  self.stdscr.keypad(True)
  self.stdscr.timeout(100)
  self.init_colors()
  while self.running:
   self.drain_replies()
   self.draw()
   key = self.stdscr.getch()
   if key == -1:
    continue
   self.handle_key(key)

 def init_colors(self) -> None:
  self.color_attrs = {"default": curses.A_NORMAL}
  if not curses.has_colors():
   return
  curses.start_color()
  curses.use_default_colors()
  for pair_id, (name, fg) in enumerate(COLOR_NAMES.items(), start=1):
   curses.init_pair(pair_id, fg, -1)
   self.color_attrs[name] = curses.color_pair(pair_id)

 def color(self, key: str, fallback: str = "default") -> int:
  name = self.config.color.get(key) or fallback
  return self.color_attrs.get(name, self.color_attrs.get(fallback, curses.A_NORMAL))

 def key_matches(self, action: str, key: int) -> bool:
  spec = self.config.keybindings.get(action, "")
  names = [part for part in re.split(r"[ ,]+", spec) if part]
  return any(key in KEY_NAMES.get(name, ()) for name in names)

 def draw(self) -> None:
  self.stdscr.erase()
  height, width = self.stdscr.getmaxyx()
  header = self.header(width)
  self.addn(0, 0, header, width, self.color("header") | curses.A_BOLD)
  body_height = max(1, height - 3)
  lines = self.render_messages(max(20, width - 2))
  max_scroll = max(0, len(lines) - body_height)
  self.scroll = max(0, min(self.scroll, max_scroll))
  start = max(0, len(lines) - body_height - self.scroll)
  visible = lines[start:start + body_height]
  for idx, (text, attr) in enumerate(visible, start=1):
   self.addn(idx, 0, text, width, attr)
  self.addn(height - 2, 0, "─" * width, width, self.color("divider"))
  self.draw_input_bar(height - 1, width)
  self.stdscr.refresh()

 def draw_input_bar(self, y: int, width: int) -> None:
  prompt = ""
  if self.input_text:
   self.addn(y, 0, self.input_text, width, self.color("input"))
   cursor_x = min(width - 1, len(self.input_text))
  else:
   ctx = f"{self.context_percent}%"
   usage = f"{self.usage_percent}%"
   self.addn(y, 0, ctx, width, self.color("context") | curses.A_BOLD)
   self.addn(y, len(ctx) + 1, usage, width, self.color("usage") | curses.A_BOLD)
   cursor_x = len(prompt)
  self.stdscr.move(y, min(width - 1, cursor_x))

 def header(self, width: int) -> str:
  if self.busy:
   elapsed = int(time.time() - self.busy_started)
   status = f"thinking {elapsed}s"
  else:
   status = self.status
  model = self.runner.model or "default"
  return f" {APP_NAME}  model {model}  {status}"[:width]

 def render_messages(self, width: int) -> list[tuple[str, int]]:
  if not self.transcript.messages:
   return [("Hermes standalone Python TUI", curses.A_BOLD), (self.help_text, self.color("help"))]
  out: list[tuple[str, int]] = []
  for msg in self.transcript.messages:
   color = self.color(msg.role)
   text = msg.text.rstrip() or " "
   wrapped = []
   for para in text.splitlines() or [""]:
    wrapped.extend(textwrap.wrap(para, width=max(8, width), replace_whitespace=False) or [""])
   out.append((wrapped[0], color))
   for line in wrapped[1:]:
    out.append((line, color))
   out.append(("", curses.A_NORMAL))
  return out

 def addn(self, y: int, x: int, text: str, width: int, attr: int = curses.A_NORMAL) -> None:
  try:
   self.stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
  except curses.error:
   pass

 def refresh_context_percent(self) -> None:
  self.context_percent = self.transcript.context_percent()

 def handle_key(self, key: int) -> None:
  if key in (curses.KEY_RESIZE,):
   return
  if self.key_matches("scroll_up", key):
   self.scroll += 1
   return
  if self.key_matches("scroll_down", key):
   self.scroll = max(0, self.scroll - 1)
   return
  if self.key_matches("page_up", key):
   self.scroll += 10
   return
  if self.key_matches("page_down", key):
   self.scroll = max(0, self.scroll - 10)
   return
  if self.key_matches("submit", key):
   self.submit()
   return
  if self.key_matches("quit", key) or key == 3:
   self.running = False
   return
  if self.key_matches("backspace", key):
   self.input_text = self.input_text[:-1]
   return
  if 0 <= key < 256:
   ch = chr(key)
   if ch.isprintable():
    self.input_text += ch

 def submit(self) -> None:
  text = self.input_text.strip()
  self.input_text = ""
  if not text:
   return
  if text.startswith(":") or text.startswith("/"):
   self.handle_command(text)
   return
  if self.busy:
   self.transcript.append("system", "Hermes is already thinking. Wait for the current reply.")
   return
  self.transcript.append("user", text)
  self.refresh_context_percent()
  prompt = self.transcript.prompt_with_context(text)
  self.busy = True
  self.busy_started = time.time()
  self.status = "sending"
  thread = threading.Thread(target=self.worker, args=(prompt,), daemon=True)
  thread.start()

 def worker(self, prompt: str) -> None:
  reply = self.runner.ask(prompt)
  self.reply_queue.put(("assistant", reply))

 def drain_replies(self) -> None:
  while True:
   try:
    role, text = self.reply_queue.get_nowait()
   except queue.Empty:
    return
   self.transcript.append(role, text)
   self.refresh_context_percent()
   self.busy = False
   self.status = "ready"

 def restart(self) -> None:
  try:
   curses.endwin()
  except curses.error:
   pass
  os.execv(sys.executable, [sys.executable, *sys.argv])

 def handle_command(self, text: str) -> None:
  raw = text.strip()
  if raw[:1] in (":", "/"):
   raw = raw[1:]
  name, _, rest = raw.partition(" ")
  cmd = f":{name}"
  if cmd in (":q", ":quit", ":exit"):
   self.running = False
   return
  if cmd in (":r", ":restart"):
   self.status = "restarting"
   self.restart()
   return
  if cmd in (":help", ":h"):
   self.transcript.append("system", self.help_text)
   return
  if cmd in (":new", ":reset"):
   self.transcript.clear()
   self.refresh_context_percent()
   self.status = "new transcript"
   return
  if cmd == ":clear":
   self.scroll = 0
   self.status = "screen cleared"
   return
  if cmd == ":model":
   if rest.strip():
    self.runner.model = rest.strip()
    self.status = f"model {self.runner.model}"
   else:
    self.transcript.append("system", f"model {self.runner.model or 'default'}")
   return
  if cmd == ":provider":
   if rest.strip():
    self.runner.provider = rest.strip()
    self.status = f"provider {self.runner.provider}"
   else:
    self.transcript.append("system", f"provider {self.runner.provider or 'default'}")
   return
  if cmd == ":tools":
   if rest.strip():
    self.runner.toolsets = rest.strip()
    self.status = f"toolsets {self.runner.toolsets}"
   else:
    self.transcript.append("system", f"toolsets {self.runner.toolsets or 'default'}")
   return
  self.transcript.append("system", f"unknown command: {cmd}")


def default_state_dir() -> Path:
 configured = os.environ.get("TORTOISE_HOME") or os.environ.get("HERMI_HOME")
 if configured:
  return Path(configured).expanduser()
 return launch_user_home() / ".local" / "state" / "tortoise"


def build_parser() -> argparse.ArgumentParser:
 parser = argparse.ArgumentParser(description="Standalone Python Hermes TUI")
 parser.add_argument("--hermes-bin", default=default_hermes_bin())
 parser.add_argument("--model")
 parser.add_argument("--provider")
 parser.add_argument("--toolsets")
 parser.add_argument("--transcript", type=Path, default=default_state_dir() / "transcript.jsonl")
 parser.add_argument("--config", type=Path, default=default_state_dir() / "config")
 parser.add_argument("--print-command", action="store_true", help="print the Hermes command shape and exit")
 return parser


def main(argv: list[str] | None = None) -> int:
 args = build_parser().parse_args(argv)
 runner = HermesRunner(args.hermes_bin, args.model, args.provider, args.toolsets)
 if args.print_command:
  print(" ".join(shlex.quote(part) for part in runner.command("hello")))
  return 0
 transcript = Transcript(args.transcript)
 config = TortoiseConfig.load(args.config)
 curses.wrapper(lambda stdscr: TortoiseApp(stdscr, transcript, runner, config).run())
 return 0


if __name__ == "__main__":
 raise SystemExit(main())
