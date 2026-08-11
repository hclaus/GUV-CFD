# Resuming a Claude Code session (VSCode extension)

Quick reference for picking a previous conversation back up.

**Easiest way:**
1. Click **Session history** at the top of the Claude Code panel.
2. Find the conversation (search by keyword, or filter by "Today" / "Last 7 days" / etc.).
3. Click it to resume with the full message history intact.

**Shortcuts:**
- `Ctrl+Shift+T` — reopens your most recent session directly.
- Spark icon in the left Activity Bar — browse all sessions.
- `Ctrl+Shift+Esc` — start a genuinely new conversation in a separate tab (alongside the
  current one, not instead of it).
- Status bar (bottom-right) — click **✱ Claude Code** as another way to open the panel.
- Command Palette (`Ctrl+Shift+P`) → "Claude Code" → e.g. "Open in New Tab".

**Cloud sessions** (started from claude.ai, not this machine): Session history → **Web**
tab → click one to download and continue locally.

**CLI fallback** (if working from an integrated terminal instead of the panel):
`claude --resume` picks a session interactively.

Note: even a brand-new session (not resumed) in this project picks up relevant context
automatically via the auto-memory files in
`C:\Users\hukcl\.claude\projects\c--Users-hukcl-Documents-Python-GUV-CFD\memory\` - useful
as a fallback if a specific session transcript is ever unavailable, though it's a summary,
not the full conversation.

Source: https://code.claude.com/docs/en/vs-code.md
