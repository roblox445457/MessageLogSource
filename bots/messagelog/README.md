# MessageLog

MessageLog is a Discord utility bot that records message deletions, verified edits, bulk deletions, and optionally new messages and member joins/leaves. New-message logging is configurable per server and off by default. It stores per-server configuration and event records in SQLite, with interactive configuration and log browsers.

## What it records—and Discord's limits

- Deleted message text and attachments are recorded only when Discord supplies a cached message to the bot. Uncached deletions are still recorded as events, but the author/content may be unavailable.
- Message edits are logged only when a cached before-state can be compared with Discord's update and a text or attachment change is verified. Metadata-only updates, unchanged content, and edits without a cached before-state do not create edit alerts.
- Attachment URLs and metadata are captured when available. Discord-hosted attachment URLs may later stop working; MessageLog does not download or preserve attachment files.
- Bulk deletion is logged per message when the event is delivered. Cached entries include their known content; uncached entries remain metadata-only.
- The bot does not claim to retrieve messages or content that Discord did not provide.
- `/snipe` shows the latest deletion captured for the current channel while the bot is running. It is private to the caller and uses the same Discord cache limits; the in-memory snipe cache clears when the bot restarts.
- Ordinary new messages are recorded only when a server administrator enables **New messages** in `/config`. This setting is off by default, and bot/webhook messages are excluded.

## Requirements

- Python 3.10 or newer
- A Discord application and bot user
- A location to run the bot continuously

## Discord Developer Portal setup

1. Create an application at the Discord Developer Portal and add a bot user.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** and **Server Members Intent**. The bot also uses the standard Guilds and Guild Messages intents.
3. Invite the bot with the OAuth2 scopes `bot` and `applications.commands`.
4. Grant these bot permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Channels (needed only when `/create channel` must create a new channel; it reuses an existing MessageLog channel)
5. Copy the bot token into an environment secret named `DISCORD_BOT_TOKEN`. Do not commit it or paste it into chat.

## Run locally

```bash
cd bots/messagelog
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Set `DISCORD_BOT_TOKEN` in `.env` (or export it in your shell), then start the bot:

```bash
python bot.py
```

The bot creates `messagelog.sqlite3` automatically and registers its global application commands on first startup. Global command updates can take a short time to appear in Discord.

### Environment variables

| Variable | Required | Purpose |
|---|---:|---|
| `DISCORD_BOT_TOKEN` | Yes | Bot authentication token; keep it in a secret store |
| `DATABASE_PATH` | No | SQLite file path; defaults to `messagelog.sqlite3` |
| `SUPPORT_URL` | No | HTTPS support-server or help link displayed by `/support` |
| `BOT_VERSION` | No | Version displayed by `/info`; defaults to `1.0.0` |
| `FEEDBACK_GUILD_ID` | No | Feedback destination server; defaults to `1561421141636157541` |
| `FEEDBACK_CHANNEL_ID` | No | Feedback destination text channel; defaults to `1561421143636971562` |
| `FEEDBACK_OWNER_IDS` | No | Comma-separated Discord user IDs allowed to reply; defaults to `1230660770749087796,1354784283796967444` |

In this Replit project, add `DISCORD_BOT_TOKEN` through **Secrets** and start the **MessageLog Bot** workflow. It launches the bot from `bots/messagelog/bot.py`. Guild configurations, message events, feedback submissions, cooldowns, and owner replies are committed to the same SQLite database (`messagelog.sqlite3` by default, or the path in `DATABASE_PATH`); they survive bot restarts as long as that file remains on persistent storage. An ephemeral deployment filesystem can lose its database on restart/redeploy, so configure a persistent database path and keep backups if deploying that way. For a larger or multi-instance deployment, move persistence to PostgreSQL before scaling out.

### Feedback setup, privacy, and replies

- Keep the feedback destination IDs and owner allowlist in the environment variables above. The default values are the MessageLog feedback server, its feedback channel, and the two authorized owners.
- The bot must be a member of the configured feedback server, and it needs **View Channel**, **Send Messages**, and **Embed Links** in the configured text channel. Keep that channel visible only to the people trusted to read user feedback.
- Users can submit through `/feedback` or the **Send Feedback** button in `/help` and `/support`. Submissions are stored in SQLite before delivery; if Discord cannot deliver one, MessageLog keeps it pending and retries after reconnecting.
- Each Discord user may submit once per rolling 24 hours across all servers using this bot. The cooldown is stored in SQLite and survives bot restarts.
- Feedback is sent to the configured channel with a reply button. Only the configured owner IDs can use the button. Replies are sent by DM and include the original feedback. If a user has DMs closed, the submission remains stored and the owner can try again later.
- Feedback records include the submitter's Discord ID, name, optional source server, text, and any reply. Anyone with access to the configured feedback channel can read the delivered submission.
- Feedback submissions remain in SQLite until manually removed; the message-log retention setting does not apply to feedback.

## First-time server setup

1. Run `/create channel` to create a private `#message-logs` channel and enable logging, or use `/set channel` and select an existing text channel.
2. Open `/config` to see status and select tracked events. Message delete/edit and bulk-delete events start enabled; new-message logging and member join/leave events start disabled. `/create channel` reuses its existing MessageLog channel and will not create another one.
3. Use `/logs test` to confirm the bot can send embeds.
4. Use `/logs status` to review the current settings.

Only a member with **Manage Server** or **Administrator** can change configuration. Log-reading commands are available to those admins and to members who can view the configured log channel. Private administrative command responses are ephemeral.

## Slash-command reference

| Command | What it does | Access |
|---|---|---|
| `/set channel channel:#channel` | Set an existing text log destination | Manage Server / Administrator |
| `/create channel` | Create or reuse the one MessageLog destination and enable logging | Manage Server / Administrator; bot needs Manage Channels only when creating |
| `/remove channel` | Remove destination and disable logging | Manage Server / Administrator |
| `/channel` | Show the current channel and settings | Admin or configured-channel access |
| `/logs enable` | Enable configured event types | Manage Server / Administrator |
| `/logs disable` | Pause new event logging | Manage Server / Administrator |
| `/logs status` | Show logging status and tracked events | Admin or configured-channel access |
| `/logs test` | Send a test embed to the destination | Manage Server / Administrator |
| `/message view record_id:123` | Open a stored log record | Admin or configured-channel access |
| `/message search` | Search content; optionally filter member, channel, event type, and result count | Admin or configured-channel access |
| `/message history member:@user` | Browse activity for a member, channel, or both | Admin or configured-channel access |
| `/message edits message_id:123` | Browse recorded edits for a Discord message ID | Admin or configured-channel access |
| `/message deleted` | Browse deleted-message records | Admin or configured-channel access |
| `/snipe` | Privately show the latest deletion captured in the current channel | Server members |
| `/member info member:@user` | Show member information | Server members |
| `/member logs member:@user` | Browse stored activity for a member | Admin or configured-channel access |
| `/member count` | Show the current server member count | Server members |
| `/help` | Open the interactive command guide | Server members |
| `/info` | Show version, uptime, and gateway latency | Server members |
| `/support` | Open command/setup help, with an optional support-server link | Server members |
| `/feedback` | Open the feedback submission form | Server members |
| `/config` | Open the interactive settings panel | Manage Server / Administrator |

The configuration panel includes working controls to enable/disable logging, choose tracked event types, set a channel by ID, create a channel, and open the command guide. Its event selector includes new messages (off by default), deletions, verified edits, bulk deletions, joins, and leaves. Log cards include buttons for stored details, member details, and help. Search/history browsers include previous/next navigation, details, and close controls. The help and support panels include the **Send Feedback** modal control.

## Operational notes

- The database is SQLite and is initialized automatically. Configure an appropriate persistent disk/file path for the deployment.
- The bot checks permissions again when an administrator invokes a sensitive command or component; command visibility in Discord is not the security boundary.
- New-message logging is an opt-in per-server event and starts disabled. When enabled, message text and attachment metadata are sent to the configured log channel and stored in SQLite; readers of that channel can see them. Bot and webhook messages are skipped.
- `/snipe` is channel-specific, ephemeral, and in-memory. It can show that a deletion occurred without content when the message was not cached by Discord; it cannot recover uncached content.
- Metadata-only Discord message updates do not create edit logs. If the old message was not cached, MessageLog cannot verify a real edit and skips that alert.
- Feedback cooldown records are durable in SQLite. Feedback queued because its destination is unavailable is retried on a later bot connection.
- Interaction buttons on already-posted log embeds remain active for 15 minutes per running process; use the slash commands to retrieve records after an interaction expires or the bot restarts.
- Retention defaults to 30 days. Expired records are deleted for a guild when its next record is written.
- Avoid enabling privileged intents unless they are enabled in the Developer Portal and the bot is authorized for them.
