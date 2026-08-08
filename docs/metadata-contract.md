# Metadata Contract

What the integration puts in front of your agent on every user message.

Every user message that reaches the agent through the integration includes two
(untrusted) metadata blocks. Agents **should** read these to understand the
conversation context.

### Sender (untrusted metadata)

```json
{
  "label": "webchat",
  "id": "webchat"
}
```

- Always `"webchat"`: the Gateway client label for the integration WebSocket
  connection. This is a fixed value; the Gateway client-id registry does not
  accept custom labels for operator clients.
- Marked "untrusted" because values are not verified by the Gateway.
- Tells the agent the message came through the OWUI pipe, not another surface.

### Conversation info (untrusted metadata)

```json
{
  "chat_id": "11111111-2222-4333-8444-555555555555",
  "source": "openwebui",
  "user_id": "66666666-7777-4888-8999-000000000000"
}
```

| Field | Type | Always present | Description |
|-------|------|----------------|-------------|
| `chat_id` | string (UUID v4) | ✅ | The OWUI conversation UUID. Stable for the lifetime of the chat. |
| `source` | string | ✅ | Always `"openwebui"`. **Use this, not Sender, to detect OWUI origin.** |
| `user_id` | string (UUID v4) | ⚠️ | The OWUI user UUID. **Omitted** when OWUI supplies no user id, the integration's internal fallback is the literal `unknown`, which is never emitted. Treat as optional. |

> The whole **Conversation info** block is emitted only when a `chat_id` is
> available. Agents should handle its absence rather than assume it.

### Usage patterns

- **Detect OWUI:** check `Conversation info.source == "openwebui"` (not Sender)
- **Get chat UUID:** extract `chat_id` for OWUI REST API calls (read history,
  check files)
- **Session key:** the integration derives
  `agent:{AGENT_ID}:openwebui-{user_id}-{chat_id}` (`AGENT_ID` is `main` by
  default; a non-default agent changes the key),
  matching the `chat_id` here
- **Distinguish surfaces:** OWUI has Conversation info; Discord has
  `Sessions` prefix; Control UI has neither

### Stability guarantee

The field types, names, and presence guarantees in this contract are stable
across releases unless a major version bump indicates otherwise. Changes
will be documented here.
