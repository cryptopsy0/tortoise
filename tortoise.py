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


class TortoiseApp:
 def __init__(self, stdscr, transcript: Transcript, runner: HermesRunner) -> None:
  self.stdscr = stdscr
  self.transcript = transcript
  self.runner = runner
  self.input_text = ""
  self.scroll = 0
  self.status = "ready"
  self.reply_queue: queue.Queue[tuple[str, str]] = queue.Queue()
  self.running = True
  self.busy = False
  self.busy_started = 0.0
  self.help_text = (
   "/help commands | /new clear transcript | /clear clear screen | "
   "/model show model | /quit exit"
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
  if not curses.has_colors():
   return
  curses.start_color()
  curses.use_default_colors()
  curses.init_pair(1, curses.COLOR_CYAN, -1)
  curses.init_pair(2, curses.COLOR_GREEN, -1)
  curses.init_pair(3, curses.COLOR_YELLOW, -1)
  curses.init_pair(4, curses.COLOR_RED, -1)
  curses.init_pair(5, curses.COLOR_BLUE, -1)

 def draw(self) -> None:
  self.stdscr.erase()
  height, width = self.stdscr.getmaxyx()
  header = self.header(width)
  self.addn(0, 0, header, width, curses.color_pair(1) | curses.A_BOLD)
  body_height = max(1, height - 3)
  lines = self.render_messages(max(20, width - 2))
  max_scroll = max(0, len(lines) - body_height)
  self.scroll = max(0, min(self.scroll, max_scroll))
  start = max(0, len(lines) - body_height - self.scroll)
  visible = lines[start:start + body_height]
  for idx, (text, attr) in enumerate(visible, start=1):
   self.addn(idx, 0, text, width, attr)
  prompt = "> " + self.input_text
  self.addn(height - 2, 0, "─" * width, width, curses.color_pair(5))
  self.addn(height - 1, 0, prompt, width, curses.color_pair(2))
  cursor_x = min(width - 1, len(prompt))
  self.stdscr.move(height - 1, cursor_x)
  self.stdscr.refresh()

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
   return [("Hermes standalone Python TUI", curses.A_BOLD), (self.help_text, curses.color_pair(3))]
  out: list[tuple[str, int]] = []
  for msg in self.transcript.messages:
   color = curses.color_pair(2) if msg.role == "user" else curses.color_pair(0)
   if msg.role == "system":
    color = curses.color_pair(3)
   label = f"{msg.role} "
   text = msg.text.rstrip() or " "
   wrapped = []
   for para in text.splitlines() or [""]:
    wrapped.extend(textwrap.wrap(para, width=max(8, width - 2), replace_whitespace=False) or [""])
   out.append((label + wrapped[0], color | curses.A_BOLD))
   for line in wrapped[1:]:
    out.append((" " + line, color))
   out.append(("", curses.A_NORMAL))
  return out

 def addn(self, y: int, x: int, text: str, width: int, attr: int = curses.A_NORMAL) -> None:
  try:
   self.stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
  except curses.error:
   pass

 def handle_key(self, key: int) -> None:
  if key in (curses.KEY_RESIZE,):
   return
  if key in (curses.KEY_UP,):
   self.scroll += 1
   return
  if key in (curses.KEY_DOWN,):
   self.scroll = max(0, self.scroll - 1)
   return
  if key in (curses.KEY_PPAGE,):
   self.scroll += 10
   return
  if key in (curses.KEY_NPAGE,):
   self.scroll = max(0, self.scroll - 10)
   return
  if key in (10, 13):
   self.submit()
   return
  if key in (3, 27):
   self.running = False
   return
  if key in (curses.KEY_BACKSPACE, 127, 8):
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
  if text.startswith("/"):
   self.handle_command(text)
   return
  if self.busy:
   self.transcript.append("system", "Hermes is already thinking. Wait for the current reply.")
   return
  self.transcript.append("user", text)
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
   self.busy = False
   self.status = "ready"

 def handle_command(self, text: str) -> None:
  cmd, _, rest = text.partition(" ")
  if cmd in ("/q", "/quit", "/exit"):
   self.running = False
   return
  if cmd in ("/help", "/h"):
   self.transcript.append("system", self.help_text)
   return
  if cmd in ("/new", "/reset"):
   self.transcript.clear()
   self.status = "new transcript"
   return
  if cmd == "/clear":
   self.scroll = 0
   self.status = "screen cleared"
   return
  if cmd == "/model":
   if rest.strip():
    self.runner.model = rest.strip()
    self.status = f"model {self.runner.model}"
   else:
    self.transcript.append("system", f"model {self.runner.model or 'default'}")
   return
  if cmd == "/provider":
   if rest.strip():
    self.runner.provider = rest.strip()
    self.status = f"provider {self.runner.provider}"
   else:
    self.transcript.append("system", f"provider {self.runner.provider or 'default'}")
   return
  if cmd == "/tools":
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
 parser.add_argument("--print-command", action="store_true", help="print the Hermes command shape and exit")
 return parser


def main(argv: list[str] | None = None) -> int:
 args = build_parser().parse_args(argv)
 runner = HermesRunner(args.hermes_bin, args.model, args.provider, args.toolsets)
 if args.print_command:
  print(" ".join(shlex.quote(part) for part in runner.command("hello")))
  return 0
 transcript = Transcript(args.transcript)
 curses.wrapper(lambda stdscr: TortoiseApp(stdscr, transcript, runner).run())
 return 0


if __name__ == "__main__":
 raise SystemExit(main())
