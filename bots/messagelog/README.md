# MessageLog

MessageLog is a Discord utility bot that records message deletions, edits, bulk deletions, and optionally member joins and leaves. It stores per-server configuration and event records in SQLite, with interactive configuration and log browsers.

## What it records—and Discord's limits

- Deleted message text and attachments are recorded only when Discord supplies a cached message to the bot. Uncached deletions are still recorded as events, but the author/content may be unavailable.
- Message edits preserve the previous content when the message was cached, and the updated content when Discord supplies it. If the old message was not cached, the log marks that limitation.
- Attachment URLs and metadata are captured when available. Discord-hosted attachment URLs may later stop working; MessageLog does not download or preserve attachment files.
- Bulk deletion is logged per message when the event is delivered. Cached entries include their known content; uncached entries remain metadata-only.
- The bot does not claim to retrieve messages or content that Discord did not provide.

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
   - Manage Channels (only needed for `/create channel`; otherwise admins can use `/set channel`)
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

In this Replit project, add `DISCORD_BOT_TOKEN` through **Secrets** and start the **MessageLog Bot** workflow. It launches the bot from `bots/messagelog/bot.py`. Keep the SQLite file on persistent storage; an ephemeral deployment filesystem can lose its database on restart/redeploy. For a larger or multi-instance deployment, move persistence to PostgreSQL before scaling out.

## First-time server setup

1. Run `/create channel` to create a private `#message-logs` channel and enable logging, or use `/set channel` and select an existing text channel.
2. Open `/config` to see status and select tracked events. Message delete/edit and bulk-delete events start enabled; member join/leave events start disabled.
3. Use `/logs test` to confirm the bot can send embeds.
4. Use `/logs status` to review the current settings.

Only a member with **Manage Server** or **Administrator** can change configuration. Log-reading commands are available to those admins and to members who can view the configured log channel. Private administrative command responses are ephemeral.

## Slash-command reference

| Command | What it does | Access |
|---|---|---|
| `/set channel channel:#channel` | Set an existing text log destination | Manage Server / Administrator |
| `/create channel` | Create a private destination and enable logging | Manage Server / Administrator; bot needs Manage Channels |
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
| `/member info member:@user` | Show member information | Server members |
| `/member logs member:@user` | Browse stored activity for a member | Admin or configured-channel access |
| `/member count` | Show the current server member count | Server members |
| `/help` | Open the interactive command guide | Server members |
| `/info` | Show version, uptime, and gateway latency | Server members |
| `/support` | Open command/setup help, with an optional support-server link | Server members |
| `/config` | Open the interactive settings panel | Manage Server / Administrator |

The configuration panel includes working controls to enable/disable logging, choose tracked event types, set a channel by ID, create a channel, and open the command guide. Log cards include buttons for stored details, member details, and help. Search/history browsers include previous/next navigation, details, and close controls.

## Operational notes

- The database is SQLite and is initialized automatically. Configure an appropriate persistent disk/file path for the deployment.
- The bot checks permissions again when an administrator invokes a sensitive command or component; command visibility in Discord is not the security boundary.
- The bot never logs message-create events, only edits/deletions and the selected optional member events.
- Interaction buttons on already-posted log embeds remain active for 15 minutes per running process; use the slash commands to retrieve records after an interaction expires or the bot restarts.
- Retention defaults to 30 days. Expired records are deleted for a guild when its next record is written.
- Avoid enabling privileged intents unless they are enabled in the Developer Portal and the bot is authorized for them.
