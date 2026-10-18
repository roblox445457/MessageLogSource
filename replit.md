# MessageLog

MessageLog is a Discord server utility bot for logging deleted and edited messages, searching stored event history, and managing independent logging settings per server.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- **MessageLog Bot** workflow — run the Discord bot process
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secret: `DISCORD_BOT_TOKEN` — Discord bot application token
- Optional bot env: `DATABASE_PATH`, `SUPPORT_URL`, `BOT_VERSION`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `bots/messagelog/bot.py` — slash commands, event handlers, embeds, and components
- `bots/messagelog/database.py` — SQLite persistence for guild settings and event records
- `bots/messagelog/README.md` — Developer Portal, permissions, setup, limitations, and command reference
- `artifacts/api-server` — existing shared API service
- `artifacts/mockup-sandbox` — design canvas preview service

## Architecture decisions

- The Discord bot is a persistent Python process, separate from the shared HTTP API.
- SQLite is used for the initial single-process bot; move to PostgreSQL before running multiple bot instances.
- Deleted-message content is only recorded when Discord delivers a cached message to the bot.

## Product

Per-server message logging with 22 slash commands, private configuration, event search/history, cached deletion sniping, and owner-routed feedback with replies.

## User preferences

_No additional standing preferences recorded._

## Gotchas

- Discord must have Message Content and Server Members Gateway Intents enabled for the configured bot application.
- The bot's token must remain in Replit Secrets; never put it in source, chat, or a committed `.env`.
- SQLite retention cleanup runs when new records are written for a server.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
