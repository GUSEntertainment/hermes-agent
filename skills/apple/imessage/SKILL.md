---
name: imessage
description: Send and receive iMessages/SMS via the imsg CLI on macOS.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [iMessage, SMS, messaging, macOS, Apple]
prerequisites:
  commands: [hermes-imsg]
---

# iMessage

Use the installed `hermes-imsg` guard to read and send iMessage/SMS via
macOS Messages.app. It delegates read-only commands to `imsg` and atomically
claims every send before transport, so failed or ambiguous attempts are never
automatically sent twice. Do not invoke raw `imsg send`.

## Prerequisites

- **macOS** with Messages.app signed in
- Install: `brew install steipete/tap/imsg`
- Grant Full Disk Access for terminal (System Settings → Privacy → Full Disk Access)
- Grant Automation permission for Messages.app when prompted

## When to Use

- User asks to send an iMessage or text message
- Reading iMessage conversation history
- Checking recent Messages.app chats
- Sending to phone numbers or Apple IDs

## When NOT to Use

- Telegram/Discord/Slack/WhatsApp messages → use the appropriate gateway channel
- Group chat management (adding/removing members) → not supported
- Bulk/mass messaging → always confirm with user first

## Quick Reference

### List Chats

```bash
hermes-imsg chats --limit 10 --json
```

### View History

```bash
# By chat ID
hermes-imsg history --chat-id 1 --limit 20 --json

# With attachments info
hermes-imsg history --chat-id 1 --limit 20 --attachments --json
```

### Send Messages

```bash
# Text only
hermes-imsg send --to "+141****1212" --text "Hello!"

# With attachment
hermes-imsg send --to "+141****1212" --text "Check this out" --file /path/to/image.jpg

# Force iMessage or SMS
hermes-imsg send --to "+141****1212" --text "Hi" --service imessage
hermes-imsg send --to "+141****1212" --text "Hi" --service sms
```

### Watch for New Messages

```bash
hermes-imsg watch --chat-id 1 --attachments
```

## Service Options

- `--service imessage` — Force iMessage (requires recipient has iMessage)
- `--service sms` — Force SMS (green bubble)
- `--service auto` — Let Messages.app decide (default)

## Rules

1. **Always confirm recipient and message content** before sending
2. **Never send to unknown numbers** without explicit user approval
3. **Verify file paths** exist before attaching
4. **Don't spam** — rate-limit yourself

## Example Workflow

User: "Text mom that I'll be late"

```bash
# 1. Find mom's chat
hermes-imsg chats --limit 20 --json | jq '.[] | select(.displayName | contains("Mom"))'

# 2. Confirm with user: "Found Mom at +1555123456. Send 'I'll be late' via iMessage?"

# 3. Send after confirmation
hermes-imsg send --to "+155****3456" --text "I'll be late"
```
