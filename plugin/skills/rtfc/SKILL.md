---
name: rtfc
description: Read The F***ing Chat - extract focused context from a Claude Code or Codex conversation history file
argument-hint: "<prompt> [--id <session-uuid>]"
allowed-tools: Bash, Read, Grep, Glob, Task
---

# RTFC - Read The F***ing Chat

Extract focused, compacted context from a Claude Code or Codex conversation
history.

## Usage

```
/rtfc <prompt>
/rtfc <prompt> --id <session-uuid>
```

Examples:
- `/rtfc sharing modal implementation decisions`
- `/rtfc database schema changes --id 5168bd70-86c2-4e6b-8a29-ba5c9345d414`

## Instructions

Use the invocation arguments (`$ARGUMENTS` in Claude Code) as follows:

### 1. Parse arguments

Split $ARGUMENTS into:
- **prompt**: everything before `--id` (the focus topic for extraction)
- **session_id**: value after `--id` if present, otherwise `current`

If prompt is empty, abort with: "Usage: /rtfc <prompt> [--id <session-uuid>]"

### 2. Resolve the history file

Resolve history from the active client. Claude Code stores project histories
under `~/.claude/projects/`; each project directory is the absolute working
directory with `/` replaced by `-`. Codex stores rollout files under
`~/.codex/sessions/YYYY/MM/DD/`.

**Claude Code:**
```bash
ls -d ~/.claude/projects/*/ | head -20
```
Match the directory corresponding to the current working directory. For
`current`, use its most recently modified JSONL file. For an explicit ID, use
`~/.claude/projects/<project-dir>/<session_id>.jsonl`.

**Codex:**
```bash
find ~/.codex/sessions -type f -name 'rollout-*.jsonl' -print
```
For `current`, choose the most recently modified rollout whose `session_meta`
payload `cwd` matches the current workspace or repository. For an explicit ID,
choose the rollout whose filename or `session_meta.payload.id` matches it.

For either client, verify a `current` candidate has a recent timestamp and a
matching working directory. Do not silently read an unrelated session merely
because it was modified most recently.

Store the resolved absolute path as `$HISTORY_FILE`.

### 3. Extract and index messages

Read the JSONL file. Each line is a JSON object. Build a message index by processing each line:

```bash
python3 -c "
import json, sys

messages = []
with open(sys.argv[1]) as f:
    for line_num, line in enumerate(f, 1):
        try:
            entry = json.loads(line.strip())
        except json.JSONDecodeError:
            continue

        msg_type = entry.get('type', '')

        # Codex rollout message. Ignore event_msg copies to avoid duplicates.
        if msg_type == 'response_item':
            payload = entry.get('payload', {})
            if payload.get('type') != 'message':
                continue
            role = payload.get('role', '')
            if role not in ('user', 'assistant'):
                continue
            content = payload.get('content', [])
            text = ' '.join(
                block.get('text', '') for block in content
                if isinstance(block, dict) and
                block.get('type') in ('input_text', 'output_text', 'text')
            )
            messages.append({
                'line': line_num, 'role': role, 'type': msg_type,
                'preview': text[:200].replace('\\n', ' '), 'tools': '',
                'length': len(text), 'uuid': payload.get('id', ''),
                'timestamp': entry.get('timestamp', '')
            })
            continue

        # Skip non-message entries
        # Message types vary by Claude Code version: 'user'/'assistant' or 'user-message'/'assistant-message'
        if msg_type not in ('user', 'assistant', 'user-message', 'assistant-message'):
            continue

        role = msg_type.replace('-message', '')

        # Extract text content
        content = entry.get('message', {}).get('content', [])
        text_parts = []
        tool_names = []
        for block in (content if isinstance(content, list) else []):
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    text_parts.append(block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    tool_names.append(block.get('name', ''))
            elif isinstance(block, str):
                text_parts.append(block)

        text = ' '.join(text_parts)
        preview = text[:200].replace('\n', ' ') if text else ''
        tools = ', '.join(tool_names) if tool_names else ''

        messages.append({
            'line': line_num,
            'role': role,
            'type': msg_type,
            'preview': preview,
            'tools': tools,
            'length': len(text),
            'uuid': entry.get('uuid', ''),
            'timestamp': entry.get('timestamp', 0)
        })

# Output as JSON
json.dump(messages, sys.stdout, indent=2)
" "$HISTORY_FILE" > /tmp/rtfc_messages.json
```

### 4. Focus extraction

Now you have a full message index. Your job is to **read through the messages** and extract context relevant to the given **prompt**.

Do this in passes:

**Pass 1 - Scan previews**: Read `/tmp/rtfc_messages.json` and identify candidate messages by scanning previews and tool usage for relevance to the prompt.

**Pass 2 - Deep read**: For each candidate message, read the actual JSONL line from `$HISTORY_FILE` to get full content. Use `sed -n '<line>p'` or read with offset to extract specific lines.

**Pass 3 - Compact**: Synthesize the relevant information into a focused summary. Include:
- Decisions made
- Key code changes and their rationale
- Problems encountered and solutions
- Architectural choices
- Any unresolved questions or TODOs

### 5. Output format

Print the result directly to the conversation (do NOT write a file). Use this format:

```
## RTFC: <prompt>

**Source/client:** `<Claude Code or Codex>: <absolute path to HISTORY_FILE>`
**Session:** `<session-uuid>`
**Messages scanned:** <total count>
**Relevant messages:** <matched count>

### Key message indexes

| Line | Role | Preview |
|------|------|---------|
| <line_num> | user/assistant | <short description of why it's relevant> |
| ... | ... | ... |

### Extracted context

<Focused summary of the conversation relevant to the prompt.
This should read like a compacted briefing - someone should be able
to understand the full context of the topic without reading the
original conversation.>

### Open items

<Any unresolved questions, pending decisions, or TODOs found
related to the prompt. "None found." if clean.>
```

### Important rules

- NEVER write output to a file. Print everything to the conversation.
- Keep the extracted context concise but complete - aim for the minimum needed to fully understand the topic.
- If the history file is very large (>5000 messages), process in chunks of 500.
- If no messages match the prompt, say so clearly rather than hallucinating context.
- Message line numbers are 1-indexed (matching the JSONL file line numbers) so they can be used with `sed -n '<line>p' <file>` for later retrieval.
