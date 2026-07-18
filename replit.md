# nexusbot

A Discord bot built with discord.py, PostgreSQL (asyncpg), and aiohttp.

## Stack
- **Language:** Python
- **Bot framework:** discord.py
- **Database:** PostgreSQL via asyncpg
- **Other:** aiohttp, beautifulsoup4, python-dotenv

## Entry point
`bot.py`

## Environment variables needed to run
- `DISCORD_TOKEN` — Discord bot token
- `DATABASE_URL` — PostgreSQL connection string (postgresql://...)
- `BOT_OWNER_ID` — Your Discord user ID (integer)

## How to run
```
pip install -r requirements.txt
python bot.py
```

## User preferences
