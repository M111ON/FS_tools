#!/usr/bin/env python3
"""
obsidian_mcp_server.py — MCP server exposing the obsidian-memory helper as
standard MCP tools (query/search/remember/link/status/index/stop).

Lets any MCP-capable agent (FreeBuff/CodeBuff, Claude Code, Hermes, opencode)
use the SAME shared vector-memory db from any working directory.

Run via stdio (default for MCP clients):
    I:\\tools\\obsidian-memory\\.venv\\Scripts\\python.exe \\
        I:\\tools\\obsidian-memory\\obsidian_mcp_server.py

Environment:
    OBSIDIAN_VAULT   vault path (default I:\\Vaults)
"""

import os
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer

BASE = Path(__file__).resolve().parent
LAUNCHER = BASE / "obsidian_mem.cmd"
DEFAULT_VAULT = os.environ.get("OBSIDIAN_VAULT", r"I:\Vaults")

server = MCPServer("obsidian-memory")


def _run(*args: str) -> str:
    """Run the shared helper, returning combined stdout+stderr as a string."""
    cmd = ["cmd.exe", "/c", str(LAUNCHER), *args, "--vault", DEFAULT_VAULT]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    return out if out.strip() else err.strip()


@server.tool()
def query(question: str, k: int = 4) -> str:
    """Semantic search the Obsidian vault and return the top-k matching note
    content for injecting into the prompt. Use to recall past work or facts."""
    return _run("query", question, "--k", str(k))


@server.tool()
def search(question: str, k: int = 8) -> str:
    """List matching note titles + snippets + similarity for a query."""
    return _run("search", question, "--k", str(k))


@server.tool()
def remember(fact: str, file: str = "opencode-remember", tag: str = "opencode") -> str:
    """Persist a durable fact/decision/preference into Memory/<file>.md and
    re-index it so it is immediately searchable across all tools."""
    return _run("remember", fact, "--file", file, "--tag", tag)


@server.tool()
def link(title: str) -> str:
    """Return the short [[wikilink]] handle for a note matching title."""
    return _run("link", title)


@server.tool()
def status() -> str:
    """Show the vector-memory db stats (docs/chunks/embed_dim)."""
    return _run("status")


@server.tool()
def index() -> str:
    """Re-scan the vault and embed only changed files (fast; 0-changed is seconds)."""
    return _run("index")


@server.tool()
def stop() -> str:
    """Kill the auto-started llama.cpp embedding server and free memory now."""
    return _run("stop")


def _start_watchdog():
    import threading, time as _t
    ppid = os.getppid()
    def _check():
        while True:
            _t.sleep(5)
            try:
                os.kill(ppid, 0)
            except OSError:
                os._exit(0)
    t = threading.Thread(target=_check, daemon=True)
    t.start()


if __name__ == "__main__":
    _start_watchdog()
    server.run("stdio")
