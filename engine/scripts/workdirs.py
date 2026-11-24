"""Work folders of the engine in /tmp/sebra/: state that scripts and hooks keep between runs on one cloud machine.

Outside the base; nothing here is committed as is. No imports and no work at import time: trace_log.py imports this
module on every Claude Code hook event. Scripts read a folder as workdirs.<NAME> when they need it.
"""

TRACE = "/tmp/sebra/trace"        # flag `on` and session work files of the detailed trace; commit.py carries them
SESSION = "/tmp/sebra/session"    # engine version a session loaded CLAUDE.md with: session_start.py and deliver.py
DELIVER = "/tmp/sebra/deliver"    # deliver.py: summary snapshots, full summary text, routine sessions, fetch stamp
MESSAGES = "/tmp/sebra/messages"  # deliver.py hook-prompt: a file per user message; skills pass its path to agents
DRAFTS = "/tmp/sebra/drafts"      # delete.py draft: copies of removed drafts until the end of the session
