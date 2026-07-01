from dotenv import load_dotenv
load_dotenv()
import discord
from discord import app_commands
import os
import asyncpg
import asyncio
import logging
import traceback
import re
import time as _time
import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nexusbot")

BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))
logger.info(f"BOT_OWNER_ID loaded: {BOT_OWNER_ID}")

DB_URL = os.environ.get("DATABASE_URL")
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

pool = None

# Cache mémoire pour les salons de bienvenue (indépendant de la DB)
_welcome_channel_cache = {}  # guild_id (int) -> channel_id (int)
_welcome_banner_bytes = None  # bytes de l'image rognée, chargée au démarrage
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _try_load_banner():
    import io
    from PIL import Image
    _base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_base, "bienvenue.png"),
        os.path.abspath("bienvenue.png"),
        os.path.join(_base, "assets", "bienvenue.png"),
        os.path.join(_base, "attached_assets", "bienvenue_cropped.png"),
        os.path.join(_base, "attached_assets", "Bienvenue_(1)_1782864806413.png"),
    ]
    found = next((p for p in candidates if os.path.exists(p)), None)
    if not found:
        logger.warning(f"Bannière introuvable. Chemins testés: {candidates}")
        return None
    try:
        img = Image.open(found).convert("RGB")
        top, bottom = 0, img.height - 1
        while top < img.height:
            row = [img.getpixel((x, top)) for x in range(0, img.width, max(1, img.width // 50))]
            if any(r < 240 or g < 240 or b < 240 for r, g, b in row):
                break
            top += 1
        while bottom > top:
            row = [img.getpixel((x, bottom)) for x in range(0, img.width, max(1, img.width // 50))]
            if any(r < 240 or g < 240 or b < 240 for r, g, b in row):
                break
            bottom -= 1
        cropped = img.crop((0, top, img.width, bottom + 1))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        logger.info(f"Bannière chargée: {found} ({top}px haut / {img.height - bottom - 1}px bas supprimés)")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Erreur chargement bannière {found}: {e}")
        return None


def _load_config():
    try:
        if os.path.exists(_CONFIG_FILE):
            import json
            with open(_CONFIG_FILE, "r") as f:
                data = json.load(f)
            for k, v in data.get("welcome_channels", {}).items():
                _welcome_channel_cache[int(k)] = int(v)
            logger.info(f"Config chargée: {len(_welcome_channel_cache)} salon(s) de bienvenue")
    except Exception as e:
        logger.error(f"Erreur chargement config.json: {e}")


def _save_config():
    try:
        import json
        data = {"welcome_channels": {str(k): v for k, v in _welcome_channel_cache.items()}}
        with open(_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Erreur sauvegarde config.json: {e}")


_load_config()


async def init_db():
    global pool
    if not DB_URL:
        logger.error("DATABASE_URL is not set.")
        return
    try:
        try:
            pool = await asyncpg.create_pool(DB_URL, ssl='require')
            logger.info("Connected to database (SSL).")
        except Exception:
            pool = await asyncpg.create_pool(DB_URL)
            logger.info("Connected to database (no SSL).")
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_logs (
                    id SERIAL PRIMARY KEY,
                    level VARCHAR(10) NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ownerlist (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(guild_id, user_id)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS whitelist (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(guild_id, user_id)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    reason TEXT,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT NOW()
                );
            """)
            try:
                await conn.execute("ALTER TABLE blacklist DROP COLUMN IF EXISTS guild_id CASCADE")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE blacklist ADD CONSTRAINT blacklist_user_id_unique UNIQUE (user_id)")
            except Exception:
                pass
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_protections (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    protection_key TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT FALSE,
                    log_channel_id TEXT,
                    punishment TEXT DEFAULT 'ban',
                    timeout_duration TEXT DEFAULT '1h',
                    whitelist_bypass BOOLEAN DEFAULT FALSE,
                    UNIQUE(guild_id, protection_key)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS gif_spam_targets (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(guild_id, user_id)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mention_spam_targets (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(guild_id, user_id)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    ticket_type TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    channel_id TEXT,
                    claimer_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    claimed_at TIMESTAMP
                );
            """)
            try:
                await conn.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS closed_by TEXT")
                await conn.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP")
            except Exception:
                pass
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, key)
                );
            """)
        logger.info("All database tables verified/created.")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")


async def log_to_db(level, message):
    if pool:
        try:
            await pool.execute(
                "INSERT INTO bot_logs (level, message) VALUES ($1, $2)",
                level, str(message)
            )
        except Exception as e:
            logger.error(f"Failed to log to DB: {e}")


async def set_guild_setting(guild_id, key, value):
    if not pool:
        return
    try:
        await pool.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id, key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
            """,
            str(guild_id), key, str(value) if value is not None else None
        )
    except Exception as e:
        logger.error(f"Failed to set guild setting {key}: {e}")


async def get_guild_setting(guild_id, key):
    if not pool:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT value FROM guild_settings WHERE guild_id = $1 AND key = $2",
            str(guild_id), key
        )
        return row['value'] if row else None
    except Exception as e:
        logger.error(f"Failed to get guild setting {key}: {e}")
        return None


async def send_ticket_log(guild, embed):
    if not guild:
        return
    try:
        ch_id = await get_guild_setting(guild.id, 'ticket_log_channel_id')
        if not ch_id:
            return
        channel = guild.get_channel(int(ch_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(ch_id))
            except Exception:
                return
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send ticket log: {e}")


async def is_guild_licensed(guild_id):
    return True


async def check_license(interaction):
    return True


SLASH_COMMANDS = [
    {"name": "/help", "params": "", "description": "Afficher la liste des commandes du bot."},
    {"name": "/panel", "params": "", "description": "Gérer les modules du serveur."},
    {"name": "/logs", "params": "", "description": "Créer le salon de logs staff."},
    {"name": "/ticketslogs", "params": "", "description": "Créer le salon de logs des tickets."},
    {"name": "/ticket", "params": "", "description": "Envoyer le panneau d'ouverture de tickets."},
    {"name": "/blacklist", "params": "", "description": "Gérer la liste noire globale du bot."},
    {"name": "/unblacklist", "params": "", "description": "Retirer un utilisateur de la blacklist."},
    {"name": "/whitelist", "params": "", "description": "Gérer la liste blanche du serveur."},
    {"name": "/ownerlist", "params": "", "description": "Gérer la liste des créateurs du serveur."},
]

TEXT_COMMANDS = [
    {"name": ".blacklist", "params": "[user]", "description": "Gérer la liste noire du serveur."},
    {"name": ".help", "params": "", "description": "Afficher la liste des commandes du bot."},
    {"name": ".ownerlist", "params": "[user]", "description": "Gérer la liste des créateurs du serveur."},
    {"name": ".whitelist", "params": "[user]", "description": "Gérer la liste blanche du serveur."},
]


async def get_command_ids(guild):
    command_ids = {}
    try:
        commands = await bot.tree.fetch_commands(guild=guild)
        for cmd in commands:
            command_ids[cmd.name] = cmd.id
        logger.info(f"Fetched {len(command_ids)} command IDs for guild {guild.name}: {command_ids}")
    except Exception as e:
        logger.warning(f"Failed to fetch guild commands: {e}")
        try:
            commands = await bot.tree.fetch_commands()
            for cmd in commands:
                command_ids[cmd.name] = cmd.id
            logger.info(f"Fetched {len(command_ids)} global command IDs: {command_ids}")
        except Exception as e2:
            logger.error(f"Failed to fetch global commands: {e2}")
    return command_ids


def build_help_embed(command_ids=None):
    if command_ids is None:
        command_ids = {}
    slash_lines = []
    for cmd in SLASH_COMMANDS:
        cmd_name = cmd['name'].lstrip('/')
        if cmd_name in command_ids:
            mention = f"</{cmd_name}:{command_ids[cmd_name]}>"
        else:
            mention = f"`{cmd['name']}`"
        if cmd["params"]:
            slash_lines.append(f"{mention} ({cmd['params']}) - {cmd['description']}")
        else:
            slash_lines.append(f"{mention} - {cmd['description']}")

    text_lines = []
    for cmd in TEXT_COMMANDS:
        if cmd["params"]:
            text_lines.append(f"`{cmd['name']}` {cmd['params']} - {cmd['description']}")
        else:
            text_lines.append(f"`{cmd['name']}` - {cmd['description']}")

    description = "## Commandes Slash\n"
    description += "\n".join(slash_lines) + "\n\n"
    description += "## Commandes Textuelles\n"
    description += "\n".join(text_lines)

    embed = discord.Embed(
        title="Orizon - Panel",
        description=description,
        color=0x2b2d31
    )

    embed.set_footer(text="© Orizon Panel")

    return embed


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True


class NexusCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True
        if interaction.guild is None:
            return True

        # Bot owner et owner du serveur passent toujours
        if interaction.user.id == BOT_OWNER_ID or interaction.user.id == interaction.guild.owner_id:
            return True

        # Admin du serveur passe toujours (fallback si DB indispo)
        member = interaction.guild.get_member(interaction.user.id)
        if member and member.guild_permissions.administrator:
            return True

        try:
            allowed = await asyncio.wait_for(
                is_owner_or_ownerlist(interaction.guild, interaction.user.id),
                timeout=2.5
            )
        except asyncio.TimeoutError:
            logger.error("interaction_check: DB timeout")
            try:
                await interaction.response.send_message("❌ Délai dépassé lors de la vérification des droits.", ephemeral=True)
            except Exception:
                pass
            return False
        except Exception as e:
            logger.error(f"interaction_check error: {e}\n{traceback.format_exc()}")
            try:
                await interaction.response.send_message("❌ Erreur interne lors de la vérification des droits.", ephemeral=True)
            except Exception:
                pass
            return False
        if not allowed:
            try:
                await interaction.response.send_message("❌ Seuls les membres de la ownerlist peuvent utiliser les commandes du bot.", ephemeral=True)
            except Exception:
                pass
            return False
        return True


class NexusBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = NexusCommandTree(self)
        self.synced = False

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await log_to_db('info', f'Bot logged in as {self.user}')

        await self.change_presence(status=discord.Status.online, activity=None)

        # Charger et rogner la bannière de bienvenue (cherche dans plusieurs emplacements)
        global _welcome_banner_bytes
        _welcome_banner_bytes = _try_load_banner()
        if _welcome_banner_bytes:
            logger.info("Bannière de bienvenue chargée avec succès")
        else:
            logger.warning("Aucune bannière de bienvenue trouvée, embed sans image")

        # Charger les salons de bienvenue en cache mémoire dès le démarrage
        if pool:
            try:
                rows = await pool.fetch(
                    "SELECT guild_id, value FROM guild_settings WHERE key = 'welcome_channel_id'"
                )
                for row in rows:
                    _welcome_channel_cache[int(row['guild_id'])] = int(row['value'])
                logger.info(f"Welcome channel cache loaded: {len(_welcome_channel_cache)} guild(s)")
            except Exception as e:
                logger.error(f"Failed to load welcome channel cache: {e}")

        try:
            self.add_view(TicketPanelView())
            self.add_view(TicketPanelLayout())
            self.add_dynamic_items(ClaimTicketButton, CloseTicketButton)
            logger.info("Registered persistent ticket views.")
        except Exception as e:
            logger.error(f"Failed to register ticket views: {e}")

        if not self.synced:
            for guild in self.guilds:
                try:
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info(f"Synced {len(synced)} slash commands to {guild.name}")
                    await log_to_db('info', f'Synced {len(synced)} commands to {guild.name}')
                except Exception as e:
                    logger.error(f"Failed to sync to {guild.name}: {e}")
                    await log_to_db('error', f'Failed to sync to {guild.name}: {e}')
            self.synced = True

    async def on_guild_join(self, guild):
        try:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error in on_guild_join: {traceback.format_exc()}")

    async def on_guild_role_create(self, role):
        guild = role.guild
        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_create):
                if entry.target.id == role.id:
                    await send_audit_log(guild, "role", "Rôle créé",
                        f"**Rôle:** {role.mention} (`{role.name}`)\n**ID:** `{role.id}`\n**Par:** {entry.user.mention} (`{entry.user}`)")
                    break
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_role_create")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_create):
                if entry.target.id != role.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_role_create")
                if is_allowed:
                    return

                try:
                    await role.delete(reason="OZR Panel: création de rôle non autorisée")
                except Exception as e:
                    logger.error(f"Failed to delete role {role.name}: {e}")
                    await log_to_db('error', f'Failed to delete role {role.name}: {e}')

                await apply_punishment(guild, user, "anti_role_create")
                await send_protection_log(guild, "anti_role_create", user, f"{user} a créé un rôle.", role=role)
                await log_to_db('warn', f'Role creation blocked: {user} created role {role.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in role create protection: {e}")

    async def on_guild_role_delete(self, role):
        guild = role.guild
        try:
            await asyncio.sleep(0.3)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
                if entry.target.id == role.id:
                    await send_audit_log(guild, "role", "Rôle supprimé",
                        f"**Rôle:** `{role.name}`\n**ID:** `{role.id}`\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xe74c3c)
                    break
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_role_delete")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
                if entry.target.id != role.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_role_delete")
                if is_allowed:
                    return

                await apply_punishment(guild, user, "anti_role_delete")
                await send_protection_log(guild, "anti_role_delete", user, f"{user} a supprimé un rôle.", role=role)
                await log_to_db('warn', f'Role deletion blocked: {user} deleted role {role.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in role delete protection: {e}")

    async def on_guild_channel_create(self, channel):
        guild = channel.guild
        if not guild:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
                if entry.target.id == channel.id:
                    await send_audit_log(guild, "channel", "Salon créé",
                        f"**Salon:** {channel.mention} (`{channel.name}`)\n**ID:** `{channel.id}`\n**Par:** {entry.user.mention} (`{entry.user}`)")
                    break
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_channel_create")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
                if entry.target.id != channel.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_channel_create")
                if is_allowed:
                    return

                try:
                    await channel.delete(reason="OZR Panel: création de salon non autorisée")
                except Exception as e:
                    logger.error(f"Failed to delete channel {channel.name}: {e}")

                await apply_punishment(guild, user, "anti_channel_create")
                await send_protection_log(guild, "anti_channel_create", user, f"{user} a créé un salon.")
                await log_to_db('warn', f'Channel creation blocked: {user} created channel {channel.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in channel create protection: {e}")

    async def on_guild_channel_delete(self, channel):
        guild = channel.guild
        if not guild:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
                if entry.target.id == channel.id:
                    await send_audit_log(guild, "channel", "Salon supprimé",
                        f"**Salon:** `{channel.name}`\n**ID:** `{channel.id}`\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xe74c3c)
                    break
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_channel_delete")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
                if entry.target.id != channel.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_channel_delete")
                if is_allowed:
                    return

                await apply_punishment(guild, user, "anti_channel_delete")
                await send_protection_log(guild, "anti_channel_delete", user, f"{user} a supprimé un salon.")
                await log_to_db('warn', f'Channel deletion blocked: {user} deleted channel {channel.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in channel delete protection: {e}")

    async def on_guild_channel_update(self, before, after):
        guild = after.guild
        if not guild:
            return

        try:
            changes = []
            if before.name != after.name:
                changes.append(f"**Nom:** `{before.name}` → `{after.name}`")
            before_topic = before.topic or ""
            after_topic = after.topic or ""
            if hasattr(before, 'topic') and hasattr(after, 'topic') and before_topic != after_topic:
                changes.append(f"**Sujet:** `{before_topic or 'Aucun'}` → `{after_topic or 'Aucun'}`")
            before_nsfw = getattr(before, 'nsfw', None)
            after_nsfw = getattr(after, 'nsfw', None)
            if before_nsfw is not None and before_nsfw != after_nsfw:
                changes.append(f"**NSFW:** `{before_nsfw}` → `{after_nsfw}`")
            before_slowmode = getattr(before, 'slowmode_delay', None)
            after_slowmode = getattr(after, 'slowmode_delay', None)
            if before_slowmode is not None and before_slowmode != after_slowmode:
                changes.append(f"**Slowmode:** `{before_slowmode}s` → `{after_slowmode}s`")
            if changes:
                await asyncio.sleep(0.3)
                executor_str = ""
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_update):
                        if entry.target.id == after.id:
                            executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                            break
                except Exception:
                    pass
                await send_audit_log(guild, "channel", "Salon modifié",
                    f"**Salon:** {after.mention} (`{after.name}`)\n**ID:** `{after.id}`\n" + "\n".join(changes) + executor_str, color=0xf39c12)
        except Exception:
            pass

        if before.overwrites != after.overwrites:
            enabled_perm = await is_protection_enabled(guild.id, "anti_channel_perm_update")
            if enabled_perm:
                try:
                    await asyncio.sleep(0.5)
                    action = discord.AuditLogAction.overwrite_update
                    async for entry in guild.audit_logs(limit=1, action=action):
                        user = entry.user
                        if user.id == self.user.id:
                            break
                        if user.id == guild.owner_id:
                            break
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_channel_perm_update")
                        if is_allowed:
                            break

                        try:
                            await after.edit(overwrites=before.overwrites, reason="OZR Panel: modification de permissions non autorisée")
                        except Exception:
                            pass

                        await apply_punishment(guild, user, "anti_channel_perm_update")
                        await send_protection_log(guild, "anti_channel_perm_update", user, f"{user} a modifié les permissions d'un salon.")
                        await log_to_db('warn', f'Channel perm update blocked: {user} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in channel perm update protection: {e}")

        enabled = await is_protection_enabled(guild.id, "anti_channel_update")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_update):
                if entry.target.id != after.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_channel_update")
                if is_allowed:
                    return

                await apply_punishment(guild, user, "anti_channel_update")
                await send_protection_log(guild, "anti_channel_update", user, f"{user} a modifié un salon.")
                await log_to_db('warn', f'Channel update blocked: {user} updated channel {after.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in channel update protection: {e}")

    async def on_guild_update(self, before, after):
        try:
            changes = []
            if before.name != after.name:
                changes.append(f"**Nom:** `{before.name}` → `{after.name}`")
            if before.icon != after.icon:
                changes.append("**Icône modifiée**")
            if before.banner != after.banner:
                changes.append("**Bannière modifiée**")
            if before.verification_level != after.verification_level:
                changes.append(f"**Vérification:** `{before.verification_level}` → `{after.verification_level}`")
            if changes:
                await asyncio.sleep(0.3)
                executor_str = ""
                try:
                    async for entry in after.audit_logs(limit=1, action=discord.AuditLogAction.guild_update):
                        executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                        break
                except Exception:
                    pass
                await send_audit_log(after, "server", "Serveur modifié",
                    "\n".join(changes) + executor_str, color=0xf39c12)
        except Exception:
            pass

        enabled = await is_protection_enabled(after.id, "anti_server_update")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in after.audit_logs(limit=1, action=discord.AuditLogAction.guild_update):
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == after.owner_id:
                    return

                is_allowed = await should_bypass_protection(after, user.id, "anti_server_update")
                if is_allowed:
                    return

                await apply_punishment(after, user, "anti_server_update")
                await send_protection_log(after, "anti_server_update", user, f"{user} a modifié le serveur.")
                await log_to_db('warn', f'Server update blocked: {user} updated server {after.name}')
                break
        except Exception as e:
            logger.error(f"Error in server update protection: {e}")

    async def on_member_ban(self, guild, user):
        try:
            await asyncio.sleep(0.3)
            executor_str = ""
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
                if entry.target.id == user.id:
                    executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                    break
            await send_audit_log(guild, "member", "Membre banni",
                f"**Utilisateur:** `{user}` (`{user.id}`)" + executor_str, color=0xe74c3c,
                thumbnail_url=user.display_avatar.url if hasattr(user, 'display_avatar') else None)
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_ban")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
                if entry.target.id != user.id:
                    break
                executor = entry.user
                if executor.id == self.user.id:
                    return
                if executor.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, executor.id, "anti_ban")
                if is_allowed:
                    return

                try:
                    await guild.unban(user, reason="OZR Panel: bannissement non autorisé")
                except Exception:
                    pass

                await apply_punishment(guild, executor, "anti_ban")
                await send_protection_log(guild, "anti_ban", executor, f"{executor} a banni un utilisateur.", target=user)
                await log_to_db('warn', f'Ban blocked: {executor} banned {user} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in ban protection: {e}")

    async def on_member_unban(self, guild, user):
        try:
            await asyncio.sleep(0.3)
            executor_str = ""
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
                if entry.target.id == user.id:
                    executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                    break
            await send_audit_log(guild, "member", "Membre débanni",
                f"**Utilisateur:** `{user}` (`{user.id}`)" + executor_str, color=0x2ecc71,
                thumbnail_url=user.display_avatar.url if hasattr(user, 'display_avatar') else None)
        except Exception:
            pass

        enabled = await is_protection_enabled(guild.id, "anti_unban")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.unban):
                if entry.target.id != user.id:
                    break
                executor = entry.user
                if executor.id == self.user.id:
                    return
                if executor.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, executor.id, "anti_unban")
                if is_allowed:
                    return

                try:
                    await guild.ban(user, reason="OZR Panel: débannissement non autorisé")
                except Exception:
                    pass

                await apply_punishment(guild, executor, "anti_unban")
                await send_protection_log(guild, "anti_unban", executor, f"{executor} a débanni un utilisateur.", target=user)
                await log_to_db('warn', f'Unban blocked: {executor} unbanned {user} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in unban protection: {e}")

    async def on_member_remove(self, member):
        guild = member.guild
        enabled = await is_protection_enabled(guild.id, "anti_kick")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.kick):
                if entry.target.id != member.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_kick")
                if is_allowed:
                    return

                await apply_punishment(guild, user, "anti_kick")
                await send_protection_log(guild, "anti_kick", user, f"{user} a expulsé un utilisateur.", target=member)
                await log_to_db('warn', f'Kick blocked: {user} kicked {member} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in kick protection: {e}")

    async def on_webhooks_update(self, channel):
        guild = channel.guild
        if not guild:
            return
        enabled = await is_protection_enabled(guild.id, "anti_webhook_create")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.webhook_create):
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_webhook_create")
                if is_allowed:
                    return

                try:
                    webhooks = await channel.webhooks()
                    for wh in webhooks:
                        if wh.user and wh.user.id == user.id:
                            await wh.delete(reason="OZR Panel: création de webhook non autorisée")
                except Exception:
                    pass

                await apply_punishment(guild, user, "anti_webhook_create")
                await send_protection_log(guild, "anti_webhook_create", user, f"{user} a créé un webhook.")
                await log_to_db('warn', f'Webhook creation blocked: {user} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in webhook protection: {e}")

    async def on_member_join(self, member):
        if member.id == BOT_OWNER_ID:
            return

        # --- Blacklist check (nécessite pool) ---
        if not member.bot and pool:
            try:
                bl = await pool.fetchrow(
                    "SELECT id FROM blacklist WHERE user_id = $1",
                    str(member.id)
                )
                if bl:
                    try:
                        await member.ban(reason="Shield Blacklist: utilisateur blacklisté globalement")
                        await log_to_db('warn', f'Blacklisted user {member} auto-banned from {member.guild.name}')
                    except Exception as e:
                        logger.error(f"Failed to ban blacklisted user {member}: {e}")
                    return
            except Exception as e:
                logger.error(f"Blacklist check failed for {member}: {e}")

        # --- Welcome embed (indépendant du pool, utilise le cache mémoire) ---
        if not member.bot:
            try:
                # 1. Chercher dans le cache mémoire
                ch_id = _welcome_channel_cache.get(member.guild.id)
                # 2. Sinon chercher dans la DB
                if ch_id is None and pool:
                    raw = await get_guild_setting(member.guild.id, 'welcome_channel_id')
                    if raw:
                        ch_id = int(raw)
                        _welcome_channel_cache[member.guild.id] = ch_id
                if ch_id:
                    welcome_ch = member.guild.get_channel(ch_id)
                    if welcome_ch is None:
                        try:
                            welcome_ch = await member.guild.fetch_channel(ch_id)
                        except (discord.NotFound, discord.Forbidden):
                            logger.warning(f"Welcome channel {ch_id} introuvable/inaccessible pour {member.guild.name}, suppression du cache")
                            _welcome_channel_cache.pop(member.guild.id, None)
                            welcome_ch = None
                        except Exception:
                            welcome_ch = None
                    if welcome_ch:
                        try:
                            embed = discord.Embed(
                                title="👋 Bienvenue sur Orizon・Poudlard",
                                description=(
                                    "Pense à lire les <#1521534040023498832> "
                                    "et à consulter <#1521534041386516631> pour bien commencer !"
                                ),
                                color=0x2b2d31,
                            )
                            embed.set_thumbnail(url=member.display_avatar.url)
                            import io
                            banner = _welcome_banner_bytes or _try_load_banner()
                            if banner:
                                banner_file = discord.File(io.BytesIO(banner), filename="bienvenue.png")
                                embed.set_image(url="attachment://bienvenue.png")
                                await welcome_ch.send(file=banner_file, embed=embed)
                            else:
                                await welcome_ch.send(embed=embed)
                            logger.info(f"Welcome embed sent for {member} in #{welcome_ch.name}")
                        except discord.Forbidden:
                            logger.error(f"Permission manquante pour envoyer dans #{welcome_ch.name} ({member.guild.name}) — vérifie que le bot a 'Envoyer des messages' et 'Intégrer des liens'")
                    else:
                        logger.warning(f"Welcome channel {ch_id} introuvable pour {member.guild.name}")
                else:
                    logger.info(f"Aucun salon de bienvenue configuré pour {member.guild.name}")
            except Exception as e:
                logger.error(f"Failed to send welcome message for {member}: {e}")

        if member.bot:
            guild = member.guild
            enabled = await is_protection_enabled(guild.id, "anti_bot_add")
            if not enabled:
                return

            try:
                await asyncio.sleep(0.5)
                async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add):
                    if entry.target.id != member.id:
                        break
                    user = entry.user
                    if user.id == self.user.id:
                        return
                    if user.id == guild.owner_id:
                        return

                    is_allowed = await should_bypass_protection(guild, user.id, "anti_bot_add")
                    if is_allowed:
                        return

                    try:
                        await member.kick(reason="OZR Panel: ajout de bot non autorisé")
                    except Exception:
                        pass

                    await apply_punishment(guild, user, "anti_bot_add")
                    await send_protection_log(guild, "anti_bot_add", user, f"{user} a ajouté un bot.")
                    await log_to_db('warn', f'Bot add blocked: {user} added bot {member} in {guild.name}')
                    break
            except Exception as e:
                logger.error(f"Error in bot add protection: {e}")

    async def on_thread_create(self, thread):
        guild = thread.guild
        if not guild:
            return
        enabled = await is_protection_enabled(guild.id, "anti_thread_create")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            user = thread.owner
            if not user:
                try:
                    user = await guild.fetch_member(thread.owner_id)
                except Exception:
                    return
            if user.id == self.user.id:
                return
            if user.id == guild.owner_id:
                return

            is_allowed = await should_bypass_protection(guild, user.id, "anti_thread_create")
            if is_allowed:
                return

            try:
                await thread.delete()
            except Exception:
                pass

            await apply_punishment(guild, user, "anti_thread_create")
            await send_protection_log(guild, "anti_thread_create", user, f"{user} a créé un fil de discussion.")
            await log_to_db('warn', f'Thread creation blocked: {user} in {guild.name}')
        except Exception as e:
            logger.error(f"Error in thread create protection: {e}")

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild

        try:
            if before.channel and not after.channel:
                await asyncio.sleep(0.3)
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_disconnect):
                        import time as _t2
                        if abs(_t2.time() - entry.created_at.timestamp()) < 5:
                            await send_audit_log(guild, "voice", "Membre déconnecté du vocal",
                                f"**Membre:** {member.mention} (`{member}`)\n**Salon:** `{before.channel.name}`\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xe74c3c,
                                thumbnail_url=member.display_avatar.url)
                        break
                except Exception:
                    pass
            if before.channel and after.channel and before.channel != after.channel:
                await asyncio.sleep(0.3)
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_move):
                        import time as _t2
                        if abs(_t2.time() - entry.created_at.timestamp()) < 5:
                            await send_audit_log(guild, "voice", "Membre déplacé",
                                f"**Membre:** {member.mention} (`{member}`)\n**De:** `{before.channel.name}`\n**Vers:** `{after.channel.name}`\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xf39c12,
                                thumbnail_url=member.display_avatar.url)
                        break
                except Exception:
                    pass
            if not before.mute and after.mute:
                await asyncio.sleep(0.3)
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                        import time as _t2
                        if entry.target.id == member.id and abs(_t2.time() - entry.created_at.timestamp()) < 5:
                            await send_audit_log(guild, "voice", "Membre mis en muet",
                                f"**Membre:** {member.mention} (`{member}`)\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xe74c3c,
                                thumbnail_url=member.display_avatar.url)
                        break
                except Exception:
                    pass
            if not before.deaf and after.deaf:
                await asyncio.sleep(0.3)
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                        import time as _t2
                        if entry.target.id == member.id and abs(_t2.time() - entry.created_at.timestamp()) < 5:
                            await send_audit_log(guild, "voice", "Membre mis en sourdine",
                                f"**Membre:** {member.mention} (`{member}`)\n**Par:** {entry.user.mention} (`{entry.user}`)", color=0xe74c3c,
                                thumbnail_url=member.display_avatar.url)
                        break
                except Exception:
                    pass
        except Exception:
            pass

        if before.channel and not after.channel and before.channel != after.channel:
            enabled = await is_protection_enabled(guild.id, "anti_disconnect")
            if enabled:
                try:
                    await asyncio.sleep(0.5)
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_disconnect):
                        user = entry.user
                        if user.id == self.user.id:
                            return
                        if user.id == guild.owner_id:
                            return
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_disconnect")
                        if is_allowed:
                            return

                        await apply_punishment(guild, user, "anti_disconnect")
                        await send_protection_log(guild, "anti_disconnect", user, f"{user} a déconnecté un utilisateur.", target=member)
                        await log_to_db('warn', f'Disconnect blocked: {user} disconnected {member} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in disconnect protection: {e}")

        if before.channel and after.channel and before.channel != after.channel:
            enabled = await is_protection_enabled(guild.id, "anti_member_move")
            if enabled:
                try:
                    await asyncio.sleep(0.5)
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_move):
                        user = entry.user
                        if user.id == self.user.id:
                            return
                        if user.id == guild.owner_id:
                            return
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_member_move")
                        if is_allowed:
                            return

                        await apply_punishment(guild, user, "anti_member_move")
                        await send_protection_log(guild, "anti_member_move", user, f"{user} a déplacé un utilisateur.", target=member)
                        await log_to_db('warn', f'Member move blocked: {user} moved {member} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in member move protection: {e}")

        if not before.mute and after.mute:
            enabled = await is_protection_enabled(guild.id, "anti_mute")
            if enabled:
                try:
                    await asyncio.sleep(0.5)
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                        if entry.target.id != member.id:
                            break
                        user = entry.user
                        if user.id == self.user.id:
                            return
                        if user.id == guild.owner_id:
                            return
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_mute")
                        if is_allowed:
                            return

                        try:
                            await member.edit(mute=False, reason="OZR Panel: mise en muet non autorisée")
                        except Exception:
                            pass

                        await apply_punishment(guild, user, "anti_mute")
                        await send_protection_log(guild, "anti_mute", user, f"{user} a mis en muet un utilisateur.", target=member)
                        await log_to_db('warn', f'Mute blocked: {user} muted {member} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in mute protection: {e}")

    async def on_guild_emojis_update(self, guild, before, after):
        enabled = await is_protection_enabled(guild.id, "anti_emoji_update")
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            added = set(after) - set(before)
            removed = set(before) - set(after)

            if added:
                action = discord.AuditLogAction.emoji_create
            elif removed:
                action = discord.AuditLogAction.emoji_delete
            else:
                action = discord.AuditLogAction.emoji_update

            async for entry in guild.audit_logs(limit=1, action=action):
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return
                is_allowed = await should_bypass_protection(guild, user.id, "anti_emoji_update")
                if is_allowed:
                    return

                if added:
                    for emoji in added:
                        try:
                            await emoji.delete(reason="OZR Panel: modification d'emoji non autorisée")
                        except Exception:
                            pass

                await apply_punishment(guild, user, "anti_emoji_update")
                await send_protection_log(guild, "anti_emoji_update", user, f"{user} a modifié les emojis du serveur.")
                await log_to_db('warn', f'Emoji update blocked: {user} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in emoji update protection: {e}")

    async def on_guild_role_update(self, before, after):
        guild = after.guild

        try:
            changes = []
            if before.name != after.name:
                changes.append(f"**Nom:** `{before.name}` → `{after.name}`")
            if before.color != after.color:
                changes.append(f"**Couleur:** `{before.color}` → `{after.color}`")
            if before.permissions != after.permissions:
                changes.append("**Permissions modifiées**")
            if before.hoist != after.hoist:
                changes.append(f"**Affiché séparément:** `{before.hoist}` → `{after.hoist}`")
            if changes:
                await asyncio.sleep(0.3)
                executor_str = ""
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
                        if entry.target.id == after.id:
                            executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                            break
                except Exception:
                    pass
                await send_audit_log(guild, "role", "Rôle modifié",
                    f"**Rôle:** {after.mention} (`{after.name}`)\n**ID:** `{after.id}`\n" + "\n".join(changes) + executor_str, color=0xf39c12)
        except Exception:
            pass

        if before.position != after.position and before.permissions == after.permissions and before.name == after.name:
            enabled_pos = await is_protection_enabled(guild.id, "anti_role_position")
            if enabled_pos:
                try:
                    await asyncio.sleep(0.5)
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
                        if entry.target.id != after.id:
                            break
                        user = entry.user
                        if user.id == self.user.id:
                            break
                        if user.id == guild.owner_id:
                            break
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_role_position")
                        if is_allowed:
                            break

                        await apply_punishment(guild, user, "anti_role_position")
                        await send_protection_log(guild, "anti_role_position", user, f"{user} a modifié la position des rôles.", role=after)
                        await log_to_db('warn', f'Role position change blocked: {user} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in role position protection: {e}")

        dangerous_perms = [
            'administrator', 'ban_members', 'kick_members', 'manage_guild',
            'manage_roles', 'manage_channels', 'mention_everyone', 'manage_webhooks'
        ]
        if before.permissions != after.permissions:
            new_dangerous = []
            for perm_name in dangerous_perms:
                had = getattr(before.permissions, perm_name, False)
                has = getattr(after.permissions, perm_name, False)
                if not had and has:
                    new_dangerous.append(perm_name)

            if new_dangerous:
                enabled_danger = await is_protection_enabled(guild.id, "anti_role_dangerous_perm")
                if enabled_danger:
                    try:
                        await asyncio.sleep(0.3)
                        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
                            if entry.target.id != after.id:
                                break
                            user = entry.user
                            if user.id == self.user.id:
                                break
                            if user.id == guild.owner_id:
                                break
                            is_allowed = await should_bypass_protection(guild, user.id, "anti_role_dangerous_perm")
                            if is_allowed:
                                break

                            try:
                                await after.edit(permissions=before.permissions, reason="OZR Panel: permission dangereuse bloquée")
                            except Exception:
                                pass

                            perm_list = ", ".join(new_dangerous)
                            await apply_punishment(guild, user, "anti_role_dangerous_perm")
                            await send_protection_log(guild, "anti_role_dangerous_perm", user, f"{user} a ajouté des permissions dangereuses ({perm_list}).", role=after)
                            await log_to_db('warn', f'Dangerous perm blocked: {user} added {perm_list} to {after.name} in {guild.name}')
                            break
                    except Exception as e:
                        logger.error(f"Error in dangerous perm protection: {e}")
                    return

        if before.permissions == after.permissions and before.name == after.name and before.color == after.color:
            return

        enabled = await is_protection_enabled(guild.id, "anti_role_update")
        if not enabled:
            return

        try:
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.role_update):
                if entry.target.id != after.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    return
                if user.id == guild.owner_id:
                    return

                is_allowed = await should_bypass_protection(guild, user.id, "anti_role_update")
                if is_allowed:
                    return

                try:
                    await after.edit(
                        permissions=before.permissions,
                        name=before.name,
                        color=before.color,
                        reason="OZR Panel: modification non autorisée"
                    )
                except Exception as e:
                    logger.error(f"Failed to restore role {after.name}: {e}")
                    await log_to_db('error', f'Failed to restore role {after.name}: {e}')

                await apply_punishment(guild, user, "anti_role_update")
                await send_protection_log(guild, "anti_role_update", user, f"{user} a modifié un rôle.", role=after)
                await log_to_db('warn', f'Role modification blocked: {user} modified role {after.name} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in role update protection: {e}")

    async def on_member_update(self, before, after):
        guild = after.guild

        try:
            if before.roles != after.roles:
                added = [r for r in after.roles if r not in before.roles]
                removed = [r for r in before.roles if r not in after.roles]
                if added or removed:
                    await asyncio.sleep(0.3)
                    executor_str = ""
                    try:
                        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_role_update):
                            if entry.target.id == after.id:
                                executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                                break
                    except Exception:
                        pass
                    desc = f"**Membre:** {after.mention} (`{after}`)\n**ID:** `{after.id}`"
                    if added:
                        desc += f"\n**Rôle(s) ajouté(s):** {', '.join(r.mention for r in added)}"
                    if removed:
                        desc += f"\n**Rôle(s) retiré(s):** {', '.join(r.mention for r in removed)}"
                    desc += executor_str
                    color = 0x2ecc71 if added and not removed else 0xe74c3c if removed and not added else 0xf39c12
                    await send_audit_log(guild, "role", "Rôle(s) modifié(s) sur un membre", desc, color=color,
                        thumbnail_url=after.display_avatar.url)

            if before.timed_out_until != after.timed_out_until and after.timed_out_until is not None:
                await asyncio.sleep(0.3)
                executor_str = ""
                try:
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                        if entry.target.id == after.id:
                            executor_str = f"\n**Par:** {entry.user.mention} (`{entry.user}`)"
                            break
                except Exception:
                    pass
                await send_audit_log(guild, "member", "Membre exclu temporairement",
                    f"**Membre:** {after.mention} (`{after}`)\n**ID:** `{after.id}`" + executor_str, color=0xe74c3c,
                    thumbnail_url=after.display_avatar.url)
        except Exception:
            pass

        if before.timed_out_until != after.timed_out_until and after.timed_out_until is not None:
            enabled = await is_protection_enabled(guild.id, "anti_timeout")
            if enabled:
                try:
                    await asyncio.sleep(0.5)
                    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                        if entry.target.id != after.id:
                            break
                        user = entry.user
                        if user.id == self.user.id:
                            break
                        if user.id == guild.owner_id:
                            break
                        is_allowed = await should_bypass_protection(guild, user.id, "anti_timeout")
                        if is_allowed:
                            break

                        try:
                            await after.timeout(None, reason="OZR Panel: exclusion temporaire non autorisée")
                        except Exception:
                            pass

                        await apply_punishment(guild, user, "anti_timeout")
                        await send_protection_log(guild, "anti_timeout", user, f"{user} a exclu temporairement un utilisateur.", target=after)
                        await log_to_db('warn', f'Timeout blocked: {user} timed out {after} in {guild.name}')
                        break
                except Exception as e:
                    logger.error(f"Error in timeout protection: {e}")

        if before.roles == after.roles:
            return

        added_roles = set(after.roles) - set(before.roles)
        removed_roles = set(before.roles) - set(after.roles)

        if added_roles:
            prot_key = "anti_role_add"
        elif removed_roles:
            prot_key = "anti_role_remove"
        else:
            return

        enabled = await is_protection_enabled(guild.id, prot_key)
        if not enabled:
            return

        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.member_role_update):
                if entry.target.id != after.id:
                    break
                user = entry.user
                if user.id == self.user.id:
                    break
                if user.id == guild.owner_id:
                    break

                is_allowed = await should_bypass_protection(guild, user.id, prot_key)
                if is_allowed:
                    break

                try:
                    if removed_roles:
                        safe_to_add = [r for r in removed_roles if r < guild.me.top_role]
                        if safe_to_add:
                            await after.add_roles(*safe_to_add, reason="OZR Panel: retrait de rôle non autorisé")
                except Exception as e:
                    logger.error(f"Failed to restore member roles for {after}: {e}")
                    await log_to_db('error', f'Failed to restore member roles for {after}: {e}')

                await apply_punishment(guild, user, prot_key)
                if added_roles:
                    for r in added_roles:
                        await send_protection_log(guild, prot_key, user, f"{user} a ajouté un rôle à un utilisateur.", role=r, target=after)
                elif removed_roles:
                    for r in removed_roles:
                        await send_protection_log(guild, prot_key, user, f"{user} a retiré un rôle à un utilisateur.", role=r, target=after)
                await log_to_db('warn', f'Member role change blocked: {user} modified roles of {after} in {guild.name}')
                break
        except Exception as e:
            logger.error(f"Error in member role protection: {e}")

    async def on_message(self, message):
        if message.author == self.user:
            return
        if message.author.bot:
            return

        is_bot_ping = message.guild and (self.user in message.mentions or (message.reference and message.reference.resolved and hasattr(message.reference.resolved, 'author') and message.reference.resolved.author == self.user))
        if is_bot_ping:
            user = message.author
            if not await can_use_bot(message.guild, user.id):
                if not hasattr(self, '_ping_tracker'):
                    self._ping_tracker = {}
                now = asyncio.get_event_loop().time()
                key = (message.guild.id, user.id)
                if key not in self._ping_tracker:
                    self._ping_tracker[key] = []
                self._ping_tracker[key] = [t for t in self._ping_tracker[key] if now - t < 15]
                self._ping_tracker[key].append(now)
                if len(self._ping_tracker[key]) >= 3:
                    self._ping_tracker[key] = []
                    member = message.guild.get_member(user.id)
                    if member:
                        try:
                            from datetime import timedelta
                            await member.timeout(timedelta(minutes=5), reason="OZR Panel: spam ping du bot")
                            embed = discord.Embed(description=f"{user.mention} a été timeout 5 minutes pour spam ping du bot.", color=0x2b2d31)
                            await message.channel.send(embed=embed)
                            await log_to_db('warn', f'{user} timed out for bot ping spam in {message.guild.name}')
                        except Exception as e:
                            logger.error(f"Failed to timeout ping spammer {user}: {e}")

        if message.guild:
            user = message.author

            link_pattern = re.compile(r'https?://\S+|discord\.gg/\S+|discord\.com/invite/\S+')
            if link_pattern.search(message.content):
                enabled = await is_protection_enabled(message.guild.id, "anti_link")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_link"):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        await apply_punishment(message.guild, user, "anti_link")
                        await send_protection_log(message.guild, "anti_link", user, f"{user} a envoyé un lien.")
                        await log_to_db('warn', f'Link blocked: {user} in {message.guild.name}')
                        return

            # --- Anti lien Discord (discord.gg / discord.com/invite) ---
            discord_invite_pattern = re.compile(
                r'(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)([a-zA-Z0-9-]+)',
                re.IGNORECASE
            )
            invite_match = discord_invite_pattern.search(message.content)
            if invite_match:
                member_obj = message.guild.get_member(user.id)
                is_admin = member_obj and member_obj.guild_permissions.administrator
                is_owner = user.id == BOT_OWNER_ID or user.id == message.guild.owner_id
                is_wl = await is_whitelisted(message.guild, user.id)
                is_ol = await is_owner_or_ownerlist(message.guild, user.id)

                if not (is_admin or is_owner or is_wl or is_ol):
                    invite_code = invite_match.group(2)

                    # Supprimer le message original
                    try:
                        await message.delete()
                    except Exception:
                        pass

                    # Remplacer le lien par des caractères aléatoires dans le chat
                    import random, string
                    chars = string.ascii_letters + string.digits + "%&#@!$"
                    def rand_replace(m):
                        length = len(m.group(0))
                        return "".join(random.choices(chars, k=length))
                    censored_content = discord_invite_pattern.sub(rand_replace, message.content)

                    try:
                        await message.channel.send(f"{user.mention}: {censored_content}")
                    except Exception:
                        pass

                    # Kick silencieux
                    if member_obj:
                        try:
                            await message.guild.kick(member_obj, reason="OZR Panel: lien Discord interdit")
                        except Exception as e:
                            logger.error(f"Failed to kick {user} for Discord invite: {e}")

                    await send_protection_log(message.guild, "anti_discord_link", user, f"{user} a envoyé un lien Discord invite.")
                    await log_to_db('warn', f'Discord invite blocked: {user} sent discord.gg/{invite_code} in {message.guild.name}')
                    return

            if len(message.mentions) >= 5:
                enabled = await is_protection_enabled(message.guild.id, "anti_mass_mention")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_mass_mention"):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        await apply_punishment(message.guild, user, "anti_mass_mention")
                        await send_protection_log(message.guild, "anti_mass_mention", user, f"{user} a mentionné massivement ({len(message.mentions)} mentions).")
                        await log_to_db('warn', f'Mass mention blocked: {user} in {message.guild.name}')
                        return

            if not hasattr(self, '_spam_tracker'):
                self._spam_tracker = {}
            now = _time.time()
            uid = user.id
            if uid not in self._spam_tracker:
                self._spam_tracker[uid] = []
            self._spam_tracker[uid] = [t for t in self._spam_tracker[uid] if now - t < 5]
            self._spam_tracker[uid].append(now)
            if len(self._spam_tracker[uid]) >= 5:
                enabled = await is_protection_enabled(message.guild.id, "anti_spam")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_spam"):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        self._spam_tracker[uid] = []
                        await apply_punishment(message.guild, user, "anti_spam")
                        await send_protection_log(message.guild, "anti_spam", user, f"{user} a envoyé du spam.")
                        await log_to_db('warn', f'Spam blocked: {user} in {message.guild.name}')
                        return

            has_gif = False
            if message.attachments:
                for att in message.attachments:
                    if att.filename and att.filename.lower().endswith('.gif'):
                        has_gif = True
                        break
            if not has_gif and message.content:
                gif_pattern = re.compile(r'https?://(?:tenor\.com|giphy\.com|media\.discordapp\.net|cdn\.discordapp\.com)\S*\.gif\S*', re.IGNORECASE)
                if gif_pattern.search(message.content):
                    has_gif = True
            if not has_gif and message.embeds:
                for emb in message.embeds:
                    if emb.type in ('gifv', 'image'):
                        has_gif = True
                        break
                    if emb.url and '.gif' in emb.url.lower():
                        has_gif = True
                        break
                    if emb.thumbnail and emb.thumbnail.url and '.gif' in emb.thumbnail.url.lower():
                        has_gif = True
                        break

            if has_gif:
                enabled = await is_protection_enabled(message.guild.id, "anti_gif_spam")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_gif_spam"):
                        is_target = False
                        if pool:
                            target_row = await pool.fetchrow(
                                "SELECT id FROM gif_spam_targets WHERE guild_id = $1 AND user_id = $2",
                                str(message.guild.id), str(user.id)
                            )
                            if target_row:
                                is_target = True
                        if is_target:
                            if not hasattr(self, '_gif_spam_tracker'):
                                self._gif_spam_tracker = {}
                            now = _time.time()
                            tracker_key = f"{message.guild.id}_{user.id}"
                            if tracker_key not in self._gif_spam_tracker:
                                self._gif_spam_tracker[tracker_key] = []
                            self._gif_spam_tracker[tracker_key] = [t for t in self._gif_spam_tracker[tracker_key] if now - t < 40]
                            self._gif_spam_tracker[tracker_key].append(now)
                            if len(self._gif_spam_tracker[tracker_key]) >= 5:
                                try:
                                    await message.delete()
                                except Exception:
                                    pass
                                self._gif_spam_tracker[tracker_key] = []
                                await apply_punishment(message.guild, user, "anti_gif_spam")
                                await send_protection_log(message.guild, "anti_gif_spam", user, f"{user} a spammé des GIFs (5 en 40s).")
                                await log_to_db('warn', f'GIF spam blocked: {user} in {message.guild.name}')
                                return

            if len(message.mentions) >= 3:
                enabled = await is_protection_enabled(message.guild.id, "anti_mention_spam")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_mention_spam"):
                        is_target = False
                        if pool:
                            target_row = await pool.fetchrow(
                                "SELECT id FROM mention_spam_targets WHERE guild_id = $1 AND user_id = $2",
                                str(message.guild.id), str(user.id)
                            )
                            if target_row:
                                is_target = True
                        if is_target:
                            if not hasattr(self, '_mention_spam_tracker'):
                                self._mention_spam_tracker = {}
                            now = _time.time()
                            tracker_key = f"{message.guild.id}_{user.id}"
                            if tracker_key not in self._mention_spam_tracker:
                                self._mention_spam_tracker[tracker_key] = []
                            self._mention_spam_tracker[tracker_key] = [t for t in self._mention_spam_tracker[tracker_key] if now - t < 8]
                            self._mention_spam_tracker[tracker_key].append(now)
                            if len(self._mention_spam_tracker[tracker_key]) >= 3:
                                try:
                                    await message.delete()
                                except Exception:
                                    pass
                                self._mention_spam_tracker[tracker_key] = []
                                await apply_punishment(message.guild, user, "anti_mention_spam")
                                await send_protection_log(message.guild, "anti_mention_spam", user, f"{user} a spammé des mentions (3+ en 8s).")
                                await log_to_db('warn', f'Mention spam blocked: {user} in {message.guild.name}')
                                return

            toxicity_words = [
                'fdp', 'ntm', 'nique', 'pute', 'connard', 'connasse',
                'enculé', 'batard', 'salope', 'merde', 'tg', 'ferme ta gueule',
                'fils de pute', 'va te faire', 'pd', 'tapette'
            ]
            msg_lower = message.content.lower()
            if any(w in msg_lower for w in toxicity_words):
                enabled = await is_protection_enabled(message.guild.id, "anti_toxicity")
                if enabled:
                    if not await should_bypass_protection(message.guild, user.id, "anti_toxicity"):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        await apply_punishment(message.guild, user, "anti_toxicity")
                        await send_protection_log(message.guild, "anti_toxicity", user, f"{user} a envoyé un message toxique.")
                        await log_to_db('warn', f'Toxicity blocked: {user} in {message.guild.name}')
                        return

        if message.content.strip().startswith(".") and message.guild:
            if not await is_owner_or_ownerlist(message.guild, message.author.id):
                embed = discord.Embed(description="❌ Seuls les membres de la ownerlist peuvent utiliser les commandes du bot.", color=0x2b2d31)
                await message.channel.send(embed=embed)
                return

        if message.content.strip().lower() == ".help":
            cmd_ids = await get_command_ids(message.guild) if message.guild else {}
            embed = build_help_embed(cmd_ids)
            await message.channel.send(embed=embed)
            await log_to_db('info', f'.help used by {message.author} in #{message.channel}')
            return

        if message.content.strip().lower().startswith(".ownerlist"):
            if not message.guild:
                return
            if not await is_bot_owner_or_server_owner(message.guild, message.author.id):
                await message.channel.send("Seul le propriétaire du bot ou le créateur du serveur peut utiliser cette commande.")
                return

            parts = message.content.strip().split()
            if len(parts) == 1:
                if pool:
                    rows = await pool.fetch(
                        "SELECT user_id FROM ownerlist WHERE guild_id = $1",
                        str(message.guild.id)
                    )
                    if not rows:
                        embed = discord.Embed(description="La ownerlist est vide.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                    else:
                        lines = [f"<@{row['user_id']}>" for row in rows]
                        embed = discord.Embed(description="\n".join(lines), color=0x2b2d31)
                        embed.set_author(name="Ownerlist")
                        await message.channel.send(embed=embed)
                return

            if len(parts) >= 2 and message.mentions:
                member = message.mentions[0]
                if member.id == message.guild.owner_id:
                    await message.channel.send("Le créateur du serveur est déjà protégé.")
                    return

                if pool:
                    existing = await pool.fetchrow(
                        "SELECT id FROM ownerlist WHERE guild_id = $1 AND user_id = $2",
                        str(message.guild.id), str(member.id)
                    )
                    if existing:
                        await pool.execute(
                            "DELETE FROM ownerlist WHERE guild_id = $1 AND user_id = $2",
                            str(message.guild.id), str(member.id)
                        )
                        embed = discord.Embed(description=f"{member.mention} a été retiré de la ownerlist.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} removed {member} from ownerlist in {message.guild.name}')
                    else:
                        await pool.execute(
                            "INSERT INTO ownerlist (guild_id, user_id) VALUES ($1, $2)",
                            str(message.guild.id), str(member.id)
                        )
                        embed = discord.Embed(description=f"{member.mention} a été ajouté à la ownerlist.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} added {member} to ownerlist in {message.guild.name}')
                return

        if message.content.strip().lower().startswith(".whitelist"):
            if not message.guild:
                return
            is_allowed = await is_owner_or_ownerlist(message.guild, message.author.id)
            if not is_allowed:
                await message.channel.send("Seul le créateur ou un membre de la ownerlist peut utiliser cette commande.")
                return

            parts = message.content.strip().split()
            if len(parts) == 1:
                if pool:
                    rows = await pool.fetch(
                        "SELECT user_id FROM whitelist WHERE guild_id = $1",
                        str(message.guild.id)
                    )
                    if not rows:
                        embed = discord.Embed(description="La whitelist est vide.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                    else:
                        lines = [f"<@{row['user_id']}>" for row in rows]
                        embed = discord.Embed(description="\n".join(lines), color=0x2b2d31)
                        embed.set_author(name="Whitelist")
                        await message.channel.send(embed=embed)
                return

            if len(parts) >= 2 and message.mentions:
                member = message.mentions[0]
                if pool:
                    existing = await pool.fetchrow(
                        "SELECT id FROM whitelist WHERE guild_id = $1 AND user_id = $2",
                        str(message.guild.id), str(member.id)
                    )
                    if existing:
                        await pool.execute(
                            "DELETE FROM whitelist WHERE guild_id = $1 AND user_id = $2",
                            str(message.guild.id), str(member.id)
                        )
                        embed = discord.Embed(description=f"{member.mention} a été retiré de la whitelist.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} removed {member} from whitelist in {message.guild.name}')
                    else:
                        await pool.execute(
                            "INSERT INTO whitelist (guild_id, user_id) VALUES ($1, $2)",
                            str(message.guild.id), str(member.id)
                        )
                        embed = discord.Embed(description=f"{member.mention} a été ajouté à la whitelist.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} added {member} to whitelist in {message.guild.name}')
                return

        if message.content.strip().lower().startswith(".blacklist"):
            if not message.guild:
                return
            is_allowed = await can_use_bot(message.guild, message.author.id)
            if not is_allowed:
                await message.channel.send("Vous ne pouvez pas utiliser le bot.")
                return

            parts = message.content.strip().split()
            if len(parts) == 1:
                embed = await build_blacklist_embed()
                await message.channel.send(embed=embed)
                return

            if len(parts) >= 2:
                target = message.mentions[0] if message.mentions else None
                if not target:
                    try:
                        uid = int(parts[1])
                    except ValueError:
                        await message.channel.send("Utilisez `.blacklist @user` ou `.blacklist <ID>`.")
                        return
                else:
                    uid = target.id

                if uid == message.author.id:
                    await message.channel.send("Vous ne pouvez pas vous blacklister vous-même.")
                    return
                if uid == bot.user.id:
                    await message.channel.send("Vous ne pouvez pas blacklister le bot.")
                    return

                if uid == BOT_OWNER_ID:
                    await message.channel.send("Vous ne pouvez pas blacklister le propriétaire du bot.")
                    return

                if pool:
                    existing = await pool.fetchrow(
                        "SELECT id, added_by FROM blacklist WHERE user_id = $1",
                        str(uid)
                    )
                    if existing:
                        added_by = existing['added_by']
                        is_bot_owner = message.author.id == BOT_OWNER_ID
                        is_guild_owner = message.guild and message.guild.owner_id == message.author.id
                        is_adder = added_by == str(message.author.id)
                        if not (is_bot_owner or is_guild_owner or is_adder):
                            await message.channel.send("❌ Seul la personne qui a blacklisté cet utilisateur, le propriétaire du bot ou le créateur du serveur peut l'unblacklist.")
                            return
                        await pool.execute("DELETE FROM blacklist WHERE user_id = $1", str(uid))
                        for guild in bot.guilds:
                            try:
                                await guild.unban(discord.Object(id=uid), reason="Shield Blacklist: retiré")
                            except Exception:
                                pass
                        embed = discord.Embed(description=f"<@{uid}> a été retiré de la blacklist et débanni.", color=0x2b2d31)
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} removed <@{uid}> from blacklist')
                    else:
                        reason = " ".join(parts[2:]) if len(parts) > 2 else None
                        await pool.execute(
                            "INSERT INTO blacklist (user_id, reason, added_by) VALUES ($1, $2, $3)",
                            str(uid), reason, str(message.author.id)
                        )
                        banned_servers = []
                        for guild in bot.guilds:
                            try:
                                await guild.ban(discord.Object(id=uid), reason=f"Shield Blacklist: {reason or 'Aucune raison'}")
                                banned_servers.append(guild.name)
                            except Exception:
                                pass
                        embed = discord.Embed(
                            description=f"<@{uid}> a bien été banni de **{len(banned_servers)}** serveur(s) avec succès.",
                            color=0x2b2d31
                        )
                        await message.channel.send(embed=embed)
                        await log_to_db('info', f'{message.author} added <@{uid}> to blacklist')
                return

    async def on_message_delete(self, message):
        if not message.guild or message.author.bot:
            return
        try:
            log_ch = await get_log_channel(message.guild, "message")
            if not log_ch:
                return
            content = message.content or "*[pas de texte]*"
            if len(content) > 1024:
                content = content[:1021] + "…"
            embed = discord.Embed(
                title="🗑️ Message supprimé",
                color=0xe74c3c,
                timestamp=datetime.datetime.utcnow()
            )
            embed.add_field(name="Auteur", value=f"{message.author.mention} (`{message.author}`)", inline=True)
            embed.add_field(name="Salon", value=message.channel.mention, inline=True)
            embed.add_field(name="Contenu", value=content, inline=False)
            if message.attachments:
                embed.add_field(name="Pièces jointes", value="\n".join(a.filename for a in message.attachments), inline=False)
            await log_ch.send(embed=embed)
        except Exception as e:
            logger.error(f"on_message_delete log error: {e}")

    async def on_message_edit(self, before, after):
        if not after.guild or after.author.bot:
            return
        if before.content == after.content:
            return
        try:
            log_ch = await get_log_channel(after.guild, "message")
            if not log_ch:
                return
            before_content = before.content or "*[pas de texte]*"
            after_content = after.content or "*[pas de texte]*"
            if len(before_content) > 512:
                before_content = before_content[:509] + "…"
            if len(after_content) > 512:
                after_content = after_content[:509] + "…"
            embed = discord.Embed(
                title="✏️ Message modifié",
                color=0xf39c12,
                timestamp=datetime.datetime.utcnow()
            )
            embed.add_field(name="Auteur", value=f"{after.author.mention} (`{after.author}`)", inline=True)
            embed.add_field(name="Salon", value=after.channel.mention, inline=True)
            embed.add_field(name="Avant", value=before_content, inline=False)
            embed.add_field(name="Après", value=after_content, inline=False)
            embed.add_field(name="Lien", value=f"[Voir le message]({after.jump_url})", inline=False)
            await log_ch.send(embed=embed)
        except Exception as e:
            logger.error(f"on_message_edit log error: {e}")

    async def on_invite_create(self, invite):
        if not invite.guild:
            return
        try:
            log_ch = await get_log_channel(invite.guild, "server")
            if not log_ch:
                return
            embed = discord.Embed(
                title="🔗 Invitation créée",
                color=0x3498db,
                timestamp=datetime.datetime.utcnow()
            )
            inviter = invite.inviter
            embed.add_field(name="Créateur", value=f"{inviter.mention} (`{inviter}`)" if inviter else "Inconnu", inline=True)
            embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
            embed.add_field(name="Salon", value=invite.channel.mention if invite.channel else "Inconnu", inline=True)
            uses_max = str(invite.max_uses) if invite.max_uses else "∞"
            expires = f"<t:{int(invite.expires_at.timestamp())}:R>" if invite.expires_at else "Jamais"
            embed.add_field(name="Utilisations max", value=uses_max, inline=True)
            embed.add_field(name="Expire", value=expires, inline=True)
            await log_ch.send(embed=embed)
        except Exception as e:
            logger.error(f"on_invite_create log error: {e}")

    async def on_guild_stickers_update(self, guild, before, after):
        try:
            log_ch = await get_log_channel(guild, "server")
            if not log_ch:
                return
            added = set(s.id for s in after) - set(s.id for s in before)
            removed = set(s.id for s in before) - set(s.id for s in after)
            if not added and not removed:
                return
            embed = discord.Embed(title="🎨 Stickers mis à jour", color=0x9b59b6, timestamp=datetime.datetime.utcnow())
            if added:
                names = [s.name for s in after if s.id in added]
                embed.add_field(name="Ajoutés", value=", ".join(names), inline=False)
            if removed:
                names = [s.name for s in before if s.id in removed]
                embed.add_field(name="Supprimés", value=", ".join(names), inline=False)
            await log_ch.send(embed=embed)
        except Exception as e:
            logger.error(f"on_guild_stickers_update log error: {e}")


bot = NexusBot()


async def is_bot_owner_or_server_owner(guild, user_id):
    if BOT_OWNER_ID and user_id == BOT_OWNER_ID:
        return True
    if guild.owner_id == user_id:
        return True
    return False


async def is_owner_or_ownerlist(guild, user_id):
    if await is_bot_owner_or_server_owner(guild, user_id):
        return True
    if pool:
        row = await pool.fetchrow(
            "SELECT id FROM ownerlist WHERE guild_id = $1 AND user_id = $2",
            str(guild.id), str(user_id)
        )
        return row is not None
    return False


async def can_use_bot(guild, user_id):
    return await is_owner_or_ownerlist(guild, user_id)


async def is_whitelisted(guild, user_id):
    if await is_owner_or_ownerlist(guild, user_id):
        return True
    if pool:
        row = await pool.fetchrow(
            "SELECT id FROM whitelist WHERE guild_id = $1 AND user_id = $2",
            str(guild.id), str(user_id)
        )
        return row is not None
    return False


async def should_bypass_protection(guild, user_id, protection_key):
    if user_id == BOT_OWNER_ID:
        return True
    if bot.user and user_id == bot.user.id:
        return True
    if await is_owner_or_ownerlist(guild, user_id):
        return True
    prot = await get_protection(guild.id, protection_key)
    if prot and prot.get('whitelist_bypass', False):
        if await is_whitelisted(guild, user_id):
            return True
    return False


async def apply_punishment(guild, user, protection_key):
    if user.id == BOT_OWNER_ID:
        return
    if bot.user and user.id == bot.user.id:
        return
    prot = await get_protection(guild.id, protection_key)
    punishment = prot['punishment'] if prot and prot['punishment'] else 'ban'
    member = guild.get_member(user.id)
    if not member:
        return

    try:
        if punishment == 'ban':
            await guild.ban(member, reason=f"OZR Panel: {protection_key}")
        elif punishment == 'kick':
            await guild.kick(member, reason=f"OZR Panel: {protection_key}")
        elif punishment == 'derank':
            roles_to_remove = [r for r in member.roles if r != guild.default_role and r < guild.me.top_role]
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"OZR Panel: {protection_key}")
        elif punishment == 'timeout':
            from datetime import timedelta
            duration_str = prot['timeout_duration'] if prot and prot.get('timeout_duration') else '1h'
            duration_map = {
                '60s': timedelta(seconds=60),
                '5m': timedelta(minutes=5),
                '10m': timedelta(minutes=10),
                '1h': timedelta(hours=1),
                '1d': timedelta(days=1),
                '1w': timedelta(weeks=1),
            }
            duration = duration_map.get(duration_str, timedelta(hours=1))
            await member.timeout(duration, reason=f"OZR Panel: {protection_key}")
    except Exception as e:
        logger.error(f"Failed to apply punishment {punishment} to {user}: {e}")
        await log_to_db('error', f'Failed to apply punishment {punishment} to {user}: {e}')


LOG_CHANNELS = {
    "role":    "logs・rôles",
    "channel": "logs・salons",
    "member":  "logs・membres",
    "voice":   "logs・vocal",
    "message": "logs・messages",
    "server":  "logs・serveur",
}
TICKET_LOG_CHANNEL = "logs・tickets"

AUDIT_LOG_CHANNELS = LOG_CHANNELS

PROTECTION_TO_CATEGORY = {
    "anti_role_add": "role", "anti_role_create": "role", "anti_role_delete": "role",
    "anti_role_remove": "role", "anti_role_update": "role", "anti_role_position": "role",
    "anti_role_dangerous_perm": "role",
    "anti_channel_create": "channel", "anti_channel_delete": "channel",
    "anti_channel_update": "channel", "anti_channel_perm_update": "channel",
    "anti_thread_create": "channel", "anti_webhook_create": "channel",
    "anti_ban": "member", "anti_unban": "member", "anti_kick": "member",
    "anti_timeout": "member", "anti_bot_add": "member",
    "anti_disconnect": "voice", "anti_member_move": "voice",
    "anti_mute": "voice", "anti_deafen": "voice",
    "anti_link": "message", "anti_spam": "message", "anti_toxicity": "message",
    "anti_embed_delete": "message", "anti_gif_spam": "message",
    "anti_mention_spam": "message", "anti_mass_mention": "message",
    "anti_server_update": "server", "anti_emoji_update": "server",
}


async def get_log_channel(guild, category_key):
    try:
        name = LOG_CHANNELS.get(category_key)
        if not name:
            return None
        cat = discord.utils.get(guild.categories, name="Logs - Général")
        if cat:
            ch = discord.utils.get(cat.text_channels, name=name)
            if ch:
                return ch
        return discord.utils.get(guild.text_channels, name=name)
    except Exception:
        return None


async def get_general_log_channel(guild):
    return await get_log_channel(guild, "server")


async def send_audit_log(guild, category_key, title, description, color=0x000000, thumbnail_url=None):
    try:
        log_ch = await get_log_channel(guild, category_key)
        if not log_ch:
            return
        embed = discord.Embed(title=title, description=description, color=0x2b2d31)
        embed.timestamp = datetime.datetime.utcnow()
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        await log_ch.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send audit log: {e}")


async def send_protection_log(guild, protection_key, user, detail_text, role=None, target=None):
    try:
        category_key = PROTECTION_TO_CATEGORY.get(protection_key, "server")
        channel = await get_log_channel(guild, category_key)
        if not channel:
            return
        prot = await get_protection(guild.id, protection_key)

        punishment_str = "Bannissement."
        if prot and prot.get('punishment'):
            for p in PUNISHMENT_OPTIONS:
                if p['value'] == prot['punishment']:
                    punishment_str = f"{p['label']}."
                    break

        perm_str = "Activé." if (prot and prot.get('enabled')) else "Désactivé."

        mention_lines = [f"**Auteur:** <@{user.id}>"]
        if target:
            mention_lines.append(f"**Cible:** <@{target.id}>")

        code_lines = [f"+ {detail_text}", f"Utilisateur: {user} (ID: {user.id})"]
        if target:
            code_lines.append(f"Cible: {target} (ID: {target.id})")
        if role:
            code_lines.append(f"Rôle: {role.name} (ID: {role.id})")
        code_lines.append(f"Punition: {punishment_str}")
        code_lines.append(f"Permission: {perm_str}")

        description = "\n".join(mention_lines) + "\n```diff\n" + "\n".join(code_lines) + "\n```"

        embed = discord.Embed(description=description, color=0x2b2d31)
        embed.timestamp = datetime.datetime.utcnow()
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send protection log: {e}")
        await log_to_db('error', f'Failed to send protection log: {e}')


async def build_ownerlist_embed(guild_id):
    if pool:
        rows = await pool.fetch(
            "SELECT user_id FROM ownerlist WHERE guild_id = $1",
            str(guild_id)
        )
        if not rows:
            embed = discord.Embed(
                description="La ownerlist est actuellement vide.\nUtilisez les boutons ci-dessous pour gérer la liste.",
                color=0x2b2d31
            )
        else:
            lines = [f"<@{row['user_id']}>" for row in rows]
            embed = discord.Embed(
                description="**Membres dans la ownerlist :**\n" + "\n".join(lines),
                color=0x2b2d31
            )
    else:
        embed = discord.Embed(description="Erreur de connexion à la base de données.", color=0x2b2d31)
    embed.set_author(name="Ownerlist")
    return embed


class OwnerlistView(discord.ui.View):
    def __init__(self, guild_id, owner_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction):
        if not await is_bot_owner_or_server_owner(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Seul le créateur du serveur peut utiliser ce menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ajouter", style=discord.ButtonStyle.green, custom_id="ownerlist_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = OwnerlistAddModal(self.guild_id, self.owner_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Retirer", style=discord.ButtonStyle.red, custom_id="ownerlist_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("Erreur de connexion.", ephemeral=True)
            return
        rows = await pool.fetch(
            "SELECT user_id FROM ownerlist WHERE guild_id = $1",
            str(self.guild_id)
        )
        if not rows:
            await interaction.response.send_message("La ownerlist est vide, rien à retirer.", ephemeral=True)
            return
        view = OwnerlistRemoveSelect(self.guild_id, self.owner_id, rows, interaction.guild)
        await interaction.response.send_message("Sélectionnez le membre à retirer :", view=view, ephemeral=True)

    @discord.ui.button(label="Liste", style=discord.ButtonStyle.blurple, custom_id="ownerlist_list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_ownerlist_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)


class OwnerlistAddModal(discord.ui.Modal, title="Ajouter à la ownerlist"):
    user_id_input = discord.ui.TextInput(
        label="ID du membre",
        placeholder="Ex: 123456789012345678",
        required=True,
        min_length=17,
        max_length=20
    )

    def __init__(self, guild_id, owner_id):
        super().__init__()
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        user_id_str = self.user_id_input.value.strip()
        try:
            uid = int(user_id_str)
        except ValueError:
            await interaction.response.send_message("ID invalide. Entrez un ID numérique.", ephemeral=True)
            return

        if uid == self.owner_id:
            await interaction.response.send_message("Le créateur du serveur est déjà protégé.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                await interaction.response.send_message("Membre introuvable sur ce serveur.", ephemeral=True)
                return

        if pool:
            existing = await pool.fetchrow(
                "SELECT id FROM ownerlist WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), str(uid)
            )
            if existing:
                await interaction.response.send_message(f"{member.mention} est déjà dans la ownerlist.", ephemeral=True)
                return

            await pool.execute(
                "INSERT INTO ownerlist (guild_id, user_id) VALUES ($1, $2)",
                str(self.guild_id), str(uid)
            )
            await log_to_db('info', f'{interaction.user} added {member} to ownerlist in {interaction.guild.name}')

            embed = await build_ownerlist_embed(self.guild_id)
            view = OwnerlistView(self.guild_id, self.owner_id)
            await interaction.response.edit_message(embed=embed, view=view)


class OwnerlistRemoveSelect(discord.ui.View):
    def __init__(self, guild_id, owner_id, rows, guild):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.owner_id = owner_id
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = guild.get_member(int(uid))
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        self.select = discord.ui.Select(placeholder="Choisir un membre à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Seul le créateur du serveur peut utiliser ce menu.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        if pool:
            await pool.execute(
                "DELETE FROM ownerlist WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from ownerlist in {interaction.guild.name}')

            embed = discord.Embed(
                description=f"<@{uid}> a été retiré de la ownerlist.",
                color=0x2b2d31
            )
            await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="ownerlist", description="Gérer la liste des créateurs du serveur.")
@app_commands.default_permissions(administrator=True)
async def ownerlist_command(interaction: discord.Interaction):
    try:
        if not await is_bot_owner_or_server_owner(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Seul le propriétaire du bot ou le créateur du serveur peut utiliser cette commande.", ephemeral=True)
            return

        embed = await build_ownerlist_embed(interaction.guild.id)
        view = OwnerlistView(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)
        await log_to_db('info', f'/ownerlist used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /ownerlist command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /ownerlist: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


async def build_whitelist_embed(guild_id):
    if pool:
        rows = await pool.fetch(
            "SELECT user_id FROM whitelist WHERE guild_id = $1",
            str(guild_id)
        )
        if not rows:
            embed = discord.Embed(
                description="La whitelist est actuellement vide.\nUtilisez les boutons ci-dessous pour gérer la liste.",
                color=0x2b2d31
            )
        else:
            lines = [f"<@{row['user_id']}>" for row in rows]
            embed = discord.Embed(
                description="**Membres dans la whitelist :**\n" + "\n".join(lines),
                color=0x2b2d31
            )
    else:
        embed = discord.Embed(description="Erreur de connexion à la base de données.", color=0x2b2d31)
    embed.set_author(name="Whitelist")
    return embed


class WhitelistView(discord.ui.View):
    def __init__(self, guild_id, owner_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Seul le créateur ou un membre de la ownerlist peut utiliser ce menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ajouter", style=discord.ButtonStyle.green, custom_id="whitelist_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = WhitelistAddModal(self.guild_id, self.owner_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Retirer", style=discord.ButtonStyle.red, custom_id="whitelist_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("Erreur de connexion.", ephemeral=True)
            return
        rows = await pool.fetch(
            "SELECT user_id FROM whitelist WHERE guild_id = $1",
            str(self.guild_id)
        )
        if not rows:
            await interaction.response.send_message("La whitelist est vide, rien à retirer.", ephemeral=True)
            return
        view = WhitelistRemoveSelect(self.guild_id, self.owner_id, rows, interaction.guild)
        await interaction.response.send_message("Sélectionnez le membre à retirer :", view=view, ephemeral=True)

    @discord.ui.button(label="Liste", style=discord.ButtonStyle.blurple, custom_id="whitelist_list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_whitelist_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)


class WhitelistAddModal(discord.ui.Modal, title="Ajouter à la whitelist"):
    user_id_input = discord.ui.TextInput(
        label="ID du membre",
        placeholder="Ex: 123456789012345678",
        required=True,
        min_length=17,
        max_length=20
    )

    def __init__(self, guild_id, owner_id):
        super().__init__()
        self.guild_id = guild_id
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        user_id_str = self.user_id_input.value.strip()
        try:
            uid = int(user_id_str)
        except ValueError:
            await interaction.response.send_message("ID invalide. Entrez un ID numérique.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                await interaction.response.send_message("Membre introuvable sur ce serveur.", ephemeral=True)
                return

        if pool:
            existing = await pool.fetchrow(
                "SELECT id FROM whitelist WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), str(uid)
            )
            if existing:
                await interaction.response.send_message(f"{member.mention} est déjà dans la whitelist.", ephemeral=True)
                return

            await pool.execute(
                "INSERT INTO whitelist (guild_id, user_id) VALUES ($1, $2)",
                str(self.guild_id), str(uid)
            )
            await log_to_db('info', f'{interaction.user} added {member} to whitelist in {interaction.guild.name}')

            embed = await build_whitelist_embed(self.guild_id)
            view = WhitelistView(self.guild_id, self.owner_id)
            await interaction.response.edit_message(embed=embed, view=view)


class WhitelistRemoveSelect(discord.ui.View):
    def __init__(self, guild_id, owner_id, rows, guild):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.owner_id = owner_id
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = guild.get_member(int(uid))
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        self.select = discord.ui.Select(placeholder="Choisir un membre à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Seul le créateur ou un membre de la ownerlist peut utiliser ce menu.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        if pool:
            await pool.execute(
                "DELETE FROM whitelist WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from whitelist in {interaction.guild.name}')

            embed = discord.Embed(
                description=f"<@{uid}> a été retiré de la whitelist.",
                color=0x2b2d31
            )
            await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="whitelist", description="Gérer la liste blanche du serveur.")
@app_commands.default_permissions(administrator=True)
async def whitelist_command(interaction: discord.Interaction):
    try:
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Seul le créateur ou un membre de la ownerlist peut utiliser cette commande.", ephemeral=True)
            return

        embed = await build_whitelist_embed(interaction.guild.id)
        view = WhitelistView(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)
        await log_to_db('info', f'/whitelist used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /whitelist command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /whitelist: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


async def is_blacklisted(user_id):
    if not pool:
        return False
    row = await pool.fetchrow(
        "SELECT id FROM blacklist WHERE user_id = $1",
        str(user_id)
    )
    return row is not None


async def build_blacklist_embed():
    embed = discord.Embed(
        title="Blacklist",
        description="Les utilisateurs blacklistés sont bannis automatiquement de tous les serveurs où le bot est présent.",
        color=0x2b2d31
    )
    if pool:
        rows = await pool.fetch("SELECT user_id, reason FROM blacklist ORDER BY added_at DESC")
        if rows:
            lines = []
            for i, row in enumerate(rows, 1):
                reason = row['reason'] or "Aucune raison"
                lines.append(f"`{i}.` <@{row['user_id']}> — {reason}")
            embed.add_field(name="Utilisateurs blacklistés", value="\n".join(lines[:20]), inline=False)
            embed.set_footer(text=f"{len(rows)} utilisateur(s) blacklisté(s)")
        else:
            embed.add_field(name="Liste vide", value="Aucun utilisateur blacklisté.", inline=False)
    return embed


class BlacklistView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction):
        if not await can_use_bot(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Vous ne pouvez pas utiliser le bot.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ajouter", style=discord.ButtonStyle.green, custom_id="blacklist_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = BlacklistAddModal(self.owner_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Retirer", style=discord.ButtonStyle.red, custom_id="blacklist_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pool:
            await interaction.response.send_message("Erreur de connexion.", ephemeral=True)
            return
        rows = await pool.fetch("SELECT user_id, added_by FROM blacklist")
        if not rows:
            await interaction.response.send_message("La blacklist est vide, rien à retirer.", ephemeral=True)
            return
        view = BlacklistRemoveSelect(self.owner_id, rows, interaction.guild)
        await interaction.response.send_message("Sélectionnez l'utilisateur à retirer :", view=view, ephemeral=True)

    @discord.ui.button(label="Liste", style=discord.ButtonStyle.blurple, custom_id="blacklist_list")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await build_blacklist_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class BlacklistAddModal(discord.ui.Modal, title="Ajouter à la blacklist"):
    user_id_input = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Ex: 123456789012345678",
        required=True,
        min_length=17,
        max_length=20
    )
    reason_input = discord.ui.TextInput(
        label="Raison",
        placeholder="Raison du blacklist (optionnel)",
        required=False,
        max_length=200
    )

    def __init__(self, owner_id):
        super().__init__()
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        user_id_str = self.user_id_input.value.strip()
        reason = self.reason_input.value.strip() or None
        try:
            uid = int(user_id_str)
        except ValueError:
            await interaction.response.send_message("ID invalide. Entrez un ID numérique.", ephemeral=True)
            return

        if uid == interaction.user.id:
            await interaction.response.send_message("Vous ne pouvez pas vous blacklister vous-même.", ephemeral=True)
            return

        if uid == bot.user.id:
            await interaction.response.send_message("Vous ne pouvez pas blacklister le bot.", ephemeral=True)
            return

        if uid == BOT_OWNER_ID:
            await interaction.response.send_message("Vous ne pouvez pas blacklister le propriétaire du bot.", ephemeral=True)
            return

        if pool:
            existing = await pool.fetchrow(
                "SELECT id FROM blacklist WHERE user_id = $1",
                str(uid)
            )
            if existing:
                await interaction.response.send_message(f"<@{uid}> est déjà dans la blacklist.", ephemeral=True)
                return

            await pool.execute(
                "INSERT INTO blacklist (user_id, reason, added_by) VALUES ($1, $2, $3)",
                str(uid), reason, str(interaction.user.id)
            )
            await log_to_db('info', f'{interaction.user} added <@{uid}> to blacklist')

            banned_servers = []
            for guild in bot.guilds:
                try:
                    member = guild.get_member(uid)
                    if not member:
                        try:
                            member = await guild.fetch_member(uid)
                        except discord.NotFound:
                            continue
                    await guild.ban(discord.Object(id=uid), reason=f"Shield Blacklist: ajouté par {interaction.user} — {reason or 'Aucune raison'}")
                    banned_servers.append(guild.name)
                except Exception as e:
                    logger.error(f"Failed to ban {uid} from {guild.name}: {e}")

            embed = discord.Embed(
                description=f"<@{uid}> a bien été banni de **{len(banned_servers)}** serveur(s) avec succès.",
                color=0x2b2d31
            )
            view = BlacklistView(self.owner_id)
            await interaction.response.edit_message(embed=embed, view=view)


class BlacklistRemoveSelect(discord.ui.View):
    def __init__(self, owner_id, rows, guild):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.added_by_map = {row['user_id']: row['added_by'] for row in rows}
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = guild.get_member(int(uid)) if guild else None
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        self.select = discord.ui.Select(placeholder="Choisir un utilisateur à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await can_use_bot(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Vous ne pouvez pas utiliser le bot.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        added_by = self.added_by_map.get(uid)
        is_bot_owner = interaction.user.id == BOT_OWNER_ID
        is_guild_owner = interaction.guild and interaction.guild.owner_id == interaction.user.id
        is_adder = added_by == str(interaction.user.id)
        if not (is_bot_owner or is_guild_owner or is_adder):
            await interaction.response.send_message(
                "❌ Seul la personne qui a blacklisté cet utilisateur, le propriétaire du bot ou le créateur du serveur peut l'unblacklist.",
                ephemeral=True
            )
            return
        if pool:
            await pool.execute(
                "DELETE FROM blacklist WHERE user_id = $1",
                uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from blacklist')

            for guild in bot.guilds:
                try:
                    await guild.unban(discord.Object(id=int(uid)), reason="Shield Blacklist: retiré de la blacklist")
                except Exception:
                    pass

            embed = discord.Embed(
                description=f"<@{uid}> a été retiré de la blacklist et débanni de tous les serveurs.",
                color=0x2b2d31
            )
            await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="blacklist", description="Gérer la blacklist globale du bot.")
@app_commands.default_permissions(administrator=True)
async def blacklist_command(interaction: discord.Interaction):
    try:
        if not await can_use_bot(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Vous ne pouvez pas utiliser le bot.", ephemeral=True)
            return

        embed = await build_blacklist_embed()
        view = BlacklistView(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)
        await log_to_db('info', f'/blacklist used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /blacklist command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /blacklist: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="unblacklist", description="Retirer un utilisateur de la blacklist.")
@app_commands.default_permissions(administrator=True)
async def unblacklist_command(interaction: discord.Interaction):
    try:
        if not await can_use_bot(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Vous ne pouvez pas utiliser le bot.", ephemeral=True)
            return

        if not pool:
            await interaction.response.send_message("Erreur de connexion à la base de données.", ephemeral=True)
            return

        rows = await pool.fetch("SELECT user_id, added_by FROM blacklist")
        if not rows:
            await interaction.response.send_message("La blacklist est vide, rien à retirer.", ephemeral=True)
            return

        view = UnblacklistSelect(interaction.user.id, rows, interaction.guild)
        await interaction.response.send_message("Sélectionnez l'utilisateur à retirer de la blacklist :", view=view, ephemeral=True)
        await log_to_db('info', f'/unblacklist used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /unblacklist command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /unblacklist: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


class UnblacklistSelect(discord.ui.View):
    def __init__(self, owner_id, rows, guild):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.added_by_map = {row['user_id']: row['added_by'] for row in rows}
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = guild.get_member(int(uid)) if guild else None
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        self.select = discord.ui.Select(placeholder="Choisir un utilisateur à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await can_use_bot(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Vous ne pouvez pas utiliser le bot.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        added_by = self.added_by_map.get(uid)
        is_bot_owner = interaction.user.id == BOT_OWNER_ID
        is_guild_owner = interaction.guild and interaction.guild.owner_id == interaction.user.id
        is_adder = added_by == str(interaction.user.id)
        if not (is_bot_owner or is_guild_owner or is_adder):
            await interaction.response.send_message(
                "❌ Seul la personne qui a blacklisté cet utilisateur, le propriétaire du bot ou le créateur du serveur peut l'unblacklist.",
                ephemeral=True
            )
            return
        if pool:
            await pool.execute(
                "DELETE FROM blacklist WHERE user_id = $1",
                uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from blacklist via /unblacklist')

            for guild in bot.guilds:
                try:
                    await guild.unban(discord.Object(id=int(uid)), reason="Shield Blacklist: retiré de la blacklist")
                except Exception:
                    pass

            embed = discord.Embed(
                description=f"<@{uid}> a été retiré de la blacklist et débanni de tous les serveurs.",
                color=0x2b2d31
            )
            await interaction.response.edit_message(content=None, embed=embed, view=None)


PROTECTION_MODULES = [
    {"key": "anti_bot_add", "label": "Ajout de bot"},
    {"key": "anti_role_add", "label": "Ajout de rôle"},
    {"key": "anti_ban", "label": "Bannissement d'utilisateur"},
    {"key": "anti_thread_create", "label": "Création de fil"},
    {"key": "anti_role_create", "label": "Création de rôle"},
    {"key": "anti_channel_create", "label": "Création de salon"},
    {"key": "anti_webhook_create", "label": "Création de webhook"},
    {"key": "anti_disconnect", "label": "Déconnexion d'utilisateur"},
    {"key": "anti_member_move", "label": "Déplacement d'un utilisateur"},
    {"key": "anti_role_remove", "label": "Enlever un rôle"},
    {"key": "anti_timeout", "label": "Exclure temporairement"},
    {"key": "anti_kick", "label": "Expulsion d'utilisateur"},
    {"key": "anti_link", "label": "Message contenant des liens"},
    {"key": "anti_spam", "label": "Message contenant du spam"},
    {"key": "anti_toxicity", "label": "Message contenant un taux de toxicité"},
    {"key": "anti_role_update", "label": "Mise à jour de rôle"},
    {"key": "anti_channel_update", "label": "Mise à jour de salon"},
    {"key": "anti_server_update", "label": "Mise à jour de serveur"},
    {"key": "anti_role_position", "label": "Mise a jour massive de la position des rôles"},
    {"key": "anti_mute", "label": "Mise en muet d'un utilisateur"},
    {"key": "anti_deafen", "label": "Mise en sourdine d'un utilisateur"},
    {"key": "anti_embed_delete", "label": "Suppression de message contenant une embed"},
    {"key": "anti_role_delete", "label": "Suppression de rôle"},
    {"key": "anti_channel_delete", "label": "Suppression de salon"},
    {"key": "anti_unban", "label": "Débannissement d'utilisateur"},
    {"key": "anti_gif_spam", "label": "Spam de GIF"},
    {"key": "anti_mention_spam", "label": "Spam de mentions"},
]

PUNISHMENT_OPTIONS = [
    {"label": "Bannissement", "value": "ban"},
    {"label": "Expulsion", "value": "kick"},
    {"label": "Retirer les rôles", "value": "derank"},
    {"label": "Exclure temporairement", "value": "timeout"},
]

TIMEOUT_DURATION_OPTIONS = [
    {"label": "60 secondes", "value": "60s"},
    {"label": "5 minutes", "value": "5m"},
    {"label": "10 minutes", "value": "10m"},
    {"label": "1 heure", "value": "1h"},
    {"label": "1 jour", "value": "1d"},
    {"label": "1 semaine", "value": "1w"},
]

ITEMS_PER_PAGE = 5

PROTECTION_TO_LOG_CHANNEL = {
    m['key']: LOG_CHANNELS.get(PROTECTION_TO_CATEGORY.get(m['key'], "server"), LOG_CHANNELS["server"])
    for m in PROTECTION_MODULES
}


async def get_protection(guild_id, key):
    if not pool:
        return None
    row = await pool.fetchrow(
        "SELECT * FROM guild_protections WHERE guild_id = $1 AND protection_key = $2",
        str(guild_id), key
    )
    return row


async def set_protection(guild_id, key, enabled=None, log_channel_id=None, punishment=None, timeout_duration=None, whitelist_bypass=None):
    if not pool:
        return
    existing = await get_protection(guild_id, key)
    if existing:
        updates = []
        params = []
        idx = 1
        if enabled is not None:
            updates.append(f"enabled = ${idx}")
            params.append(enabled)
            idx += 1
        if log_channel_id is not None:
            updates.append(f"log_channel_id = ${idx}")
            params.append(log_channel_id if log_channel_id != "" else None)
            idx += 1
        if punishment is not None:
            updates.append(f"punishment = ${idx}")
            params.append(punishment)
            idx += 1
        if timeout_duration is not None:
            updates.append(f"timeout_duration = ${idx}")
            params.append(timeout_duration)
            idx += 1
        if whitelist_bypass is not None:
            updates.append(f"whitelist_bypass = ${idx}")
            params.append(whitelist_bypass)
            idx += 1
        if updates:
            params.append(str(guild_id))
            params.append(key)
            query = f"UPDATE guild_protections SET {', '.join(updates)} WHERE guild_id = ${idx} AND protection_key = ${idx+1}"
            await pool.execute(query, *params)
    else:
        await pool.execute(
            "INSERT INTO guild_protections (guild_id, protection_key, enabled, log_channel_id, punishment, timeout_duration, whitelist_bypass) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            str(guild_id), key,
            enabled if enabled is not None else False,
            log_channel_id if log_channel_id else None,
            punishment if punishment else "ban",
            timeout_duration if timeout_duration else "1h",
            whitelist_bypass if whitelist_bypass is not None else False
        )


async def is_protection_enabled(guild_id, key):
    row = await get_protection(guild_id, key)
    if row:
        return row['enabled']
    return False


def build_panel_page_embed(protections_data, page, total_pages):
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_modules = PROTECTION_MODULES[start:end]

    lines = []
    for mod in page_modules:
        key = mod["key"]
        label = mod["label"]
        prot = protections_data.get(key)
        if prot and prot['enabled']:
            check = " ✅"
        else:
            check = ""
        lines.append(f"⏻ {label}{check}")

    embed = discord.Embed(
        description="\n\n".join(lines),
        color=0x2b2d31
    )
    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed


def build_protection_detail_embed(mod, prot, guild):
    key = mod["key"]
    label = mod["label"]

    if prot and prot['enabled']:
        state_str = "✅"
    else:
        state_str = "❌"

    log_channel_str = "Non configuré"
    if prot and prot['log_channel_id']:
        channel = guild.get_channel(int(prot['log_channel_id']))
        if channel:
            log_channel_str = f"{channel.mention}"
        else:
            log_channel_str = f"ID: {prot['log_channel_id']}"

    punishment_str = "Bannissement."
    if prot and prot['punishment']:
        for p in PUNISHMENT_OPTIONS:
            if p['value'] == prot['punishment']:
                punishment_str = f"{p['label']}."
                break

    timeout_line = ""
    if prot and prot.get('punishment') == 'timeout':
        td_val = prot.get('timeout_duration', '1h')
        td_label = next((td['label'] for td in TIMEOUT_DURATION_OPTIONS if td['value'] == td_val), td_val)
        timeout_line = f"\nDurée: {td_label}"

    permission_str = "🔒"

    whitelist_bypass = prot.get('whitelist_bypass', False) if prot else False
    whitelist_line = f"    • Utilisateur dans la liste blanche. {'✅' if whitelist_bypass else '❌'}"

    embed = discord.Embed(
        description=(
            f"**• {label}**\n"
            f"```\n"
            f"État: {state_str}\n"
            f"Logs: {log_channel_str}\n"
            f"Permission: {permission_str}\n"
            f"Punition: {punishment_str}{timeout_line}\n"
            f"Autorisé:\n"
            f"    • Utilisateur dans la liste des propriétaires. ✅\n"
            f"{whitelist_line}\n"
            f"```"
        ),
        color=0x2b2d31
    )
    return embed


class PanelView(discord.ui.View):
    def __init__(self, guild_id, owner_id, protections_data, page=0):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page
        self.total_pages = (len(PROTECTION_MODULES) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()

        start = self.page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_modules = PROTECTION_MODULES[start:end]

        options = []
        for mod in page_modules:
            key = mod["key"]
            label = mod["label"]
            prot = self.protections_data.get(key)
            if prot and prot['enabled']:
                desc = "Activé"
            else:
                desc = "Désactivé"
            options.append(discord.SelectOption(label=label, value=key, description=desc))

        select = discord.ui.Select(
            placeholder="Sélectionner un module...",
            options=options,
            custom_id="panel_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id="panel_prev", disabled=(self.page == 0))
        prev_btn.callback = self.prev_callback
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id="panel_next", disabled=(self.page >= self.total_pages - 1))
        next_btn.callback = self.next_callback
        self.add_item(next_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Vous n'êtes pas autorisé à utiliser ce menu.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        try:
            key = interaction.data['values'][0]
            mod = next((m for m in PROTECTION_MODULES if m['key'] == key), None)
            if not mod:
                return
            prot = self.protections_data.get(key)
            embed = build_protection_detail_embed(mod, prot, interaction.guild)
            detail_view = ProtectionDetailView(self.guild_id, self.owner_id, key, self.protections_data, self.page)
            await interaction.response.edit_message(embed=embed, view=detail_view)
        except Exception as e:
            logger.error(f"Error in panel select_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in panel select: {e}')
            except Exception:
                pass
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    async def prev_callback(self, interaction: discord.Interaction):
        try:
            if self.page > 0:
                self.page -= 1
                self._update_buttons()
                embed = build_panel_page_embed(self.protections_data, self.page, self.total_pages)
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Error in panel prev_callback: {traceback.format_exc()}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    async def next_callback(self, interaction: discord.Interaction):
        try:
            if self.page < self.total_pages - 1:
                self.page += 1
                self._update_buttons()
                embed = build_panel_page_embed(self.protections_data, self.page, self.total_pages)
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Error in panel next_callback: {traceback.format_exc()}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass


class ProtectionDetailView(discord.ui.View):
    def __init__(self, guild_id, owner_id, protection_key, protections_data, page):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protection_key = protection_key
        self.protections_data = protections_data
        self.page = page
        self.total_pages = (len(PROTECTION_MODULES) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        self._build_items()

    def _build_items(self):
        self.clear_items()
        prot = self.protections_data.get(self.protection_key)
        is_on = prot and prot['enabled']

        mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
        current_label = mod['label'] if mod else self.protection_key

        start = self.page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_modules = PROTECTION_MODULES[start:end]
        module_options = []
        for m in page_modules:
            is_default = (m['key'] == self.protection_key)
            module_options.append(discord.SelectOption(
                label=m['label'],
                value=m['key'],
                emoji="⚙️",
                default=is_default
            ))
        module_select = discord.ui.Select(
            placeholder=current_label,
            options=module_options,
            custom_id="prot_module_select",
            row=0
        )
        module_select.callback = self.module_select_callback
        self.add_item(module_select)

        current_punishment = prot['punishment'] if prot and prot['punishment'] else 'ban'
        punishment_options = []
        for p in PUNISHMENT_OPTIONS:
            punishment_options.append(discord.SelectOption(
                label=f"{p['label']}.",
                value=p['value'],
                default=(p['value'] == current_punishment)
            ))
        punishment_select = discord.ui.Select(
            placeholder="Bannissement.",
            options=punishment_options,
            custom_id="prot_punishment",
            row=1
        )
        punishment_select.callback = self.punishment_callback
        self.add_item(punishment_select)

        if current_punishment == 'timeout':
            current_timeout = prot['timeout_duration'] if prot and prot.get('timeout_duration') else '1h'
            timeout_options = []
            for td in TIMEOUT_DURATION_OPTIONS:
                timeout_options.append(discord.SelectOption(
                    label=td['label'],
                    value=td['value'],
                    default=(td['value'] == current_timeout)
                ))
            timeout_select = discord.ui.Select(
                placeholder="Durée de l'exclusion...",
                options=timeout_options,
                custom_id="prot_timeout_duration",
                row=2
            )
            timeout_select.callback = self.timeout_duration_callback
            self.add_item(timeout_select)

        if is_on:
            toggle_btn = discord.ui.Button(emoji="🛑", label="Désactiver", style=discord.ButtonStyle.secondary, custom_id="prot_toggle", row=3)
        else:
            toggle_btn = discord.ui.Button(emoji="🛑", label="Activer", style=discord.ButtonStyle.secondary, custom_id="prot_toggle", row=3)
        toggle_btn.callback = self.toggle_callback
        self.add_item(toggle_btn)

        wb = prot.get('whitelist_bypass', False) if prot else False
        if wb:
            wl_btn = discord.ui.Button(emoji="✅", label="Whitelist", style=discord.ButtonStyle.green, custom_id="prot_whitelist_bypass", row=3)
        else:
            wl_btn = discord.ui.Button(emoji="❌", label="Whitelist", style=discord.ButtonStyle.secondary, custom_id="prot_whitelist_bypass", row=3)
        wl_btn.callback = self.whitelist_bypass_callback
        self.add_item(wl_btn)

        log_btn = discord.ui.Button(emoji="📝", label="Logs", style=discord.ButtonStyle.secondary, custom_id="prot_logs", row=3)
        log_btn.callback = self.logs_callback
        self.add_item(log_btn)

        salon_btn = discord.ui.Button(emoji="📢", label="Salon", style=discord.ButtonStyle.primary, custom_id="prot_salon", row=3)
        salon_btn.callback = self.salon_callback
        self.add_item(salon_btn)

        if self.protection_key in ("anti_gif_spam", "anti_mention_spam"):
            targets_btn = discord.ui.Button(emoji="🎯", label="Cibles", style=discord.ButtonStyle.primary, custom_id="prot_targets", row=4)
            targets_btn.callback = self.targets_callback
            self.add_item(targets_btn)

        back_btn = discord.ui.Button(emoji="↩️", label="Retour", style=discord.ButtonStyle.danger, custom_id="prot_back", row=4)
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

    async def targets_callback(self, interaction: discord.Interaction):
        try:
            if self.protection_key == "anti_gif_spam":
                view = GifSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
                embed = await build_gif_targets_embed(self.guild_id, interaction.guild)
            else:
                view = MentionSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
                embed = await build_mention_targets_embed(self.guild_id, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error in targets_callback: {traceback.format_exc()}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    async def back_callback(self, interaction: discord.Interaction):
        try:
            embed = build_panel_page_embed(self.protections_data, self.page, self.total_pages)
            view = PanelView(self.guild_id, self.owner_id, self.protections_data, self.page)
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error in back_callback: {traceback.format_exc()}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Vous n'êtes pas autorisé à utiliser ce menu.", ephemeral=True)
            return False
        return True

    async def module_select_callback(self, interaction: discord.Interaction):
        try:
            key = interaction.data['values'][0]
            self.protection_key = key
            mod = next((m for m in PROTECTION_MODULES if m['key'] == key), None)
            if not mod:
                return
            prot = self.protections_data.get(key)
            embed = build_protection_detail_embed(mod, prot, interaction.guild)
            self._build_items()
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Error in module_select_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in module_select_callback: {e}')
            except Exception:
                pass
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
            except Exception:
                pass

    async def toggle_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            prot = self.protections_data.get(self.protection_key)
            new_state = not (prot and prot['enabled'])
            await set_protection(self.guild_id, self.protection_key, enabled=new_state)
            if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                self.protections_data[self.protection_key] = {'enabled': new_state, 'log_channel_id': None, 'punishment': 'ban'}
            else:
                self.protections_data[self.protection_key]['enabled'] = new_state
            mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
            embed = build_protection_detail_embed(mod, self.protections_data[self.protection_key], interaction.guild)
            self._build_items()
            await interaction.message.edit(embed=embed, view=self)
            state_label = "activé" if new_state else "désactivé"
            await log_to_db('info', f'{interaction.user} {state_label} {mod["label"]} dans {interaction.guild.name}')
        except Exception as e:
            logger.error(f"Error in toggle_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in toggle_callback: {e}')
            except Exception:
                pass

    async def whitelist_bypass_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            prot = self.protections_data.get(self.protection_key)
            current_wb = prot.get('whitelist_bypass', False) if prot else False
            new_wb = not current_wb
            await set_protection(self.guild_id, self.protection_key, whitelist_bypass=new_wb)
            if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                self.protections_data[self.protection_key] = {'enabled': False, 'log_channel_id': None, 'punishment': 'ban', 'timeout_duration': '1h', 'whitelist_bypass': new_wb}
            else:
                self.protections_data[self.protection_key]['whitelist_bypass'] = new_wb
            mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
            embed = build_protection_detail_embed(mod, self.protections_data[self.protection_key], interaction.guild)
            self._build_items()
            await interaction.message.edit(embed=embed, view=self)
            status = "activé" if new_wb else "désactivé"
            await log_to_db('info', f'{interaction.user} {status} whitelist bypass for {mod["label"]} in {interaction.guild.name}')
        except Exception as e:
            logger.error(f"Error in whitelist_bypass_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in whitelist_bypass_callback: {e}')
            except Exception:
                pass

    async def logs_callback(self, interaction: discord.Interaction):
        await self._auto_assign_log_channel(interaction)

    async def salon_callback(self, interaction: discord.Interaction):
        await self._auto_assign_log_channel(interaction)

    async def _auto_assign_log_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            expected_channel_name = PROTECTION_TO_LOG_CHANNEL.get(self.protection_key)
            if not expected_channel_name:
                await interaction.followup.send("Aucun salon de logs associé à cette protection.", ephemeral=True)
                return

            guild = interaction.guild
            log_ch = None
            category = discord.utils.get(guild.categories, name="Logs - Général")
            if category:
                log_ch = discord.utils.get(category.text_channels, name=expected_channel_name)

            if not log_ch:
                await interaction.followup.send(
                    f"Le salon `{expected_channel_name}` n'existe pas. Utilisez `/logs` d'abord pour créer les salons de logs.",
                    ephemeral=True
                )
                return

            prot = self.protections_data.get(self.protection_key)
            current_log = prot.get('log_channel_id') if prot else None

            if current_log == str(log_ch.id):
                await set_protection(self.guild_id, self.protection_key, log_channel_id="")
                if self.protections_data.get(self.protection_key):
                    self.protections_data[self.protection_key]['log_channel_id'] = None
                mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
                embed = build_protection_detail_embed(mod, self.protections_data.get(self.protection_key), guild)
                self._build_items()
                await interaction.message.edit(embed=embed, view=self)
                await log_to_db('info', f'{interaction.user} removed log channel for {mod["label"]} in {guild.name}')
            else:
                await set_protection(self.guild_id, self.protection_key, log_channel_id=str(log_ch.id))
                if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                    self.protections_data[self.protection_key] = {'enabled': False, 'log_channel_id': str(log_ch.id), 'punishment': 'ban'}
                else:
                    self.protections_data[self.protection_key]['log_channel_id'] = str(log_ch.id)
                mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
                embed = build_protection_detail_embed(mod, self.protections_data.get(self.protection_key), guild)
                self._build_items()
                await interaction.message.edit(embed=embed, view=self)
                await log_to_db('info', f'{interaction.user} set log channel to {log_ch.name} for {mod["label"]} in {guild.name}')
        except Exception as e:
            logger.error(f"Error in _auto_assign_log_channel: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in _auto_assign_log_channel: {e}')
            except Exception:
                pass

    async def timeout_duration_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            value = interaction.data['values'][0]
            await set_protection(self.guild_id, self.protection_key, timeout_duration=value)
            if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                self.protections_data[self.protection_key] = {'enabled': False, 'log_channel_id': None, 'punishment': 'timeout', 'timeout_duration': value}
            else:
                self.protections_data[self.protection_key]['timeout_duration'] = value
            mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
            embed = build_protection_detail_embed(mod, self.protections_data[self.protection_key], interaction.guild)
            self._build_items()
            await interaction.message.edit(embed=embed, view=self)
            td_label = next((td['label'] for td in TIMEOUT_DURATION_OPTIONS if td['value'] == value), value)
            await log_to_db('info', f'{interaction.user} set timeout duration for {mod["label"]} to {td_label} in {interaction.guild.name}')
        except Exception as e:
            logger.error(f"Error in timeout_duration_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in timeout_duration_callback: {e}')
            except Exception:
                pass

    async def punishment_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            value = interaction.data['values'][0]
            await set_protection(self.guild_id, self.protection_key, punishment=value)
            if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                self.protections_data[self.protection_key] = {'enabled': False, 'log_channel_id': None, 'punishment': value}
            else:
                self.protections_data[self.protection_key]['punishment'] = value
            mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
            embed = build_protection_detail_embed(mod, self.protections_data[self.protection_key], interaction.guild)
            self._build_items()
            await interaction.message.edit(embed=embed, view=self)
            p_label = next((p['label'] for p in PUNISHMENT_OPTIONS if p['value'] == value), value)
            await log_to_db('info', f'{interaction.user} changed punishment for {mod["label"]} to {p_label} in {interaction.guild.name}')
        except Exception as e:
            logger.error(f"Error in punishment_callback: {traceback.format_exc()}")
            try:
                await log_to_db('error', f'Error in punishment_callback: {e}')
            except Exception:
                pass


async def build_gif_targets_embed(guild_id, guild):
    lines = []
    if pool:
        rows = await pool.fetch(
            "SELECT user_id FROM gif_spam_targets WHERE guild_id = $1 ORDER BY added_at DESC",
            str(guild_id)
        )
        if rows:
            for i, row in enumerate(rows, 1):
                uid = row['user_id']
                member = guild.get_member(int(uid))
                if member:
                    lines.append(f"`{i}.` {member.mention} (`{uid}`)")
                else:
                    lines.append(f"`{i}.` Utilisateur inconnu (`{uid}`)")
        else:
            lines.append("Aucune cible configurée.")
    else:
        lines.append("Base de données indisponible.")

    embed = discord.Embed(
        title="🎯 Cibles — Spam de GIF",
        description="\n".join(lines),
        color=0x2b2d31
    )
    embed.set_footer(text="5 GIFs en 40 secondes = punition")
    return embed


class GifSpamTargetsView(discord.ui.View):
    def __init__(self, guild_id, owner_id, protections_data, page):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page
        self._build_items()

    def _build_items(self):
        self.clear_items()
        prot = self.protections_data.get("anti_gif_spam")
        current_punishment = prot['punishment'] if prot and prot['punishment'] else 'ban'

        punishment_options = []
        for p in PUNISHMENT_OPTIONS:
            punishment_options.append(discord.SelectOption(
                label=p['label'],
                value=p['value'],
                default=(p['value'] == current_punishment)
            ))
        punishment_select = discord.ui.Select(
            placeholder="Punition...",
            options=punishment_options,
            custom_id="gif_punishment",
            row=0
        )
        punishment_select.callback = self.punishment_callback
        self.add_item(punishment_select)

        if current_punishment == 'timeout':
            current_timeout = prot.get('timeout_duration', '1h') if prot else '1h'
            timeout_options = []
            for td in TIMEOUT_DURATION_OPTIONS:
                timeout_options.append(discord.SelectOption(
                    label=td['label'],
                    value=td['value'],
                    default=(td['value'] == current_timeout)
                ))
            timeout_select = discord.ui.Select(
                placeholder="Durée de l'exclusion...",
                options=timeout_options,
                custom_id="gif_timeout_dur",
                row=1
            )
            timeout_select.callback = self.timeout_duration_callback
            self.add_item(timeout_select)

        add_btn = discord.ui.Button(label="Ajouter une cible", style=discord.ButtonStyle.green, emoji="➕", custom_id="gif_add", row=2)
        add_btn.callback = self.add_target
        self.add_item(add_btn)

        remove_btn = discord.ui.Button(label="Retirer une cible", style=discord.ButtonStyle.red, emoji="➖", custom_id="gif_remove", row=2)
        remove_btn.callback = self.remove_target
        self.add_item(remove_btn)

        back_btn = discord.ui.Button(label="Retour", style=discord.ButtonStyle.danger, emoji="↩️", custom_id="gif_back", row=3)
        back_btn.callback = self.back
        self.add_item(back_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Vous n'êtes pas autorisé.", ephemeral=True)
            return False
        return True

    async def punishment_callback(self, interaction: discord.Interaction):
        value = interaction.data['values'][0]
        await set_protection(self.guild_id, "anti_gif_spam", punishment=value)
        if "anti_gif_spam" not in self.protections_data or not self.protections_data["anti_gif_spam"]:
            self.protections_data["anti_gif_spam"] = {'enabled': False, 'log_channel_id': None, 'punishment': value, 'timeout_duration': '1h'}
        else:
            self.protections_data["anti_gif_spam"]['punishment'] = value
        self._build_items()
        embed = await build_gif_targets_embed(self.guild_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    async def timeout_duration_callback(self, interaction: discord.Interaction):
        value = interaction.data['values'][0]
        await set_protection(self.guild_id, "anti_gif_spam", timeout_duration=value)
        if self.protections_data.get("anti_gif_spam"):
            self.protections_data["anti_gif_spam"]['timeout_duration'] = value
        self._build_items()
        embed = await build_gif_targets_embed(self.guild_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    async def add_target(self, interaction: discord.Interaction):
        modal = GifSpamAddTargetModal(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.send_modal(modal)

    async def remove_target(self, interaction: discord.Interaction):
        if not pool:
            await interaction.response.send_message("Base de données indisponible.", ephemeral=True)
            return
        rows = await pool.fetch(
            "SELECT user_id FROM gif_spam_targets WHERE guild_id = $1 ORDER BY added_at DESC",
            str(self.guild_id)
        )
        if not rows:
            await interaction.response.send_message("Aucune cible à retirer.", ephemeral=True)
            return
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = interaction.guild.get_member(int(uid))
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        view = GifSpamRemoveSelect(self.guild_id, self.owner_id, self.protections_data, self.page, options, interaction.guild)
        await interaction.response.edit_message(view=view)

    async def back(self, interaction: discord.Interaction):
        mod = next((m for m in PROTECTION_MODULES if m['key'] == "anti_gif_spam"), None)
        prot = self.protections_data.get("anti_gif_spam")
        embed = build_protection_detail_embed(mod, prot, interaction.guild)
        detail_view = ProtectionDetailView(self.guild_id, self.owner_id, "anti_gif_spam", self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=detail_view)


class GifSpamAddTargetModal(discord.ui.Modal, title="Ajouter une cible GIF"):
    user_id_input = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Ex: 123456789012345678",
        required=True,
        min_length=17,
        max_length=20
    )

    def __init__(self, guild_id, owner_id, protections_data, page):
        super().__init__()
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page

    async def on_submit(self, interaction: discord.Interaction):
        user_id_str = self.user_id_input.value.strip()
        try:
            uid = int(user_id_str)
        except ValueError:
            await interaction.response.send_message("ID invalide. Entrez un ID numérique.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                await interaction.response.send_message("Membre introuvable sur ce serveur.", ephemeral=True)
                return

        if pool:
            existing = await pool.fetchrow(
                "SELECT id FROM gif_spam_targets WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), str(uid)
            )
            if existing:
                await interaction.response.send_message(f"{member.mention} est déjà dans les cibles.", ephemeral=True)
                return

            await pool.execute(
                "INSERT INTO gif_spam_targets (guild_id, user_id, added_by) VALUES ($1, $2, $3)",
                str(self.guild_id), str(uid), str(interaction.user.id)
            )
            await log_to_db('info', f'{interaction.user} added {member} to GIF spam targets in {interaction.guild.name}')

        embed = await build_gif_targets_embed(self.guild_id, interaction.guild)
        view = GifSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=view)


class GifSpamRemoveSelect(discord.ui.View):
    def __init__(self, guild_id, owner_id, protections_data, page, options, guild):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page
        self.guild = guild
        self.select = discord.ui.Select(placeholder="Choisir une cible à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Vous n'êtes pas autorisé.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        if pool:
            await pool.execute(
                "DELETE FROM gif_spam_targets WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from GIF spam targets in {interaction.guild.name}')

        embed = await build_gif_targets_embed(self.guild_id, interaction.guild)
        view = GifSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=view)


async def build_mention_targets_embed(guild_id, guild):
    lines = []
    if pool:
        rows = await pool.fetch(
            "SELECT user_id FROM mention_spam_targets WHERE guild_id = $1 ORDER BY added_at DESC",
            str(guild_id)
        )
        if rows:
            for i, row in enumerate(rows, 1):
                uid = row['user_id']
                member = guild.get_member(int(uid))
                if member:
                    lines.append(f"`{i}.` {member.mention} (`{uid}`)")
                else:
                    lines.append(f"`{i}.` Utilisateur inconnu (`{uid}`)")
        else:
            lines.append("Aucune cible configurée.")
    else:
        lines.append("Base de données indisponible.")

    embed = discord.Embed(
        title="🎯 Cibles — Spam de mentions",
        description="\n".join(lines),
        color=0x2b2d31
    )
    embed.set_footer(text="3+ mentions en 8 secondes = punition")
    return embed


class MentionSpamTargetsView(discord.ui.View):
    def __init__(self, guild_id, owner_id, protections_data, page):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page
        self._build_items()

    def _build_items(self):
        self.clear_items()
        prot = self.protections_data.get("anti_mention_spam")
        current_punishment = prot['punishment'] if prot and prot['punishment'] else 'ban'

        punishment_options = []
        for p in PUNISHMENT_OPTIONS:
            punishment_options.append(discord.SelectOption(
                label=p['label'],
                value=p['value'],
                default=(p['value'] == current_punishment)
            ))
        punishment_select = discord.ui.Select(
            placeholder="Punition...",
            options=punishment_options,
            custom_id="mention_punishment",
            row=0
        )
        punishment_select.callback = self.punishment_callback
        self.add_item(punishment_select)

        if current_punishment == 'timeout':
            current_timeout = prot.get('timeout_duration', '1h') if prot else '1h'
            timeout_options = []
            for td in TIMEOUT_DURATION_OPTIONS:
                timeout_options.append(discord.SelectOption(
                    label=td['label'],
                    value=td['value'],
                    default=(td['value'] == current_timeout)
                ))
            timeout_select = discord.ui.Select(
                placeholder="Durée de l'exclusion...",
                options=timeout_options,
                custom_id="mention_timeout_dur",
                row=1
            )
            timeout_select.callback = self.timeout_duration_callback
            self.add_item(timeout_select)

        add_btn = discord.ui.Button(label="Ajouter une cible", style=discord.ButtonStyle.green, emoji="➕", custom_id="mention_add", row=2)
        add_btn.callback = self.add_target
        self.add_item(add_btn)

        remove_btn = discord.ui.Button(label="Retirer une cible", style=discord.ButtonStyle.red, emoji="➖", custom_id="mention_remove", row=2)
        remove_btn.callback = self.remove_target
        self.add_item(remove_btn)

        back_btn = discord.ui.Button(label="Retour", style=discord.ButtonStyle.danger, emoji="↩️", custom_id="mention_back", row=3)
        back_btn.callback = self.back
        self.add_item(back_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Vous n'êtes pas autorisé.", ephemeral=True)
            return False
        return True

    async def punishment_callback(self, interaction: discord.Interaction):
        value = interaction.data['values'][0]
        await set_protection(self.guild_id, "anti_mention_spam", punishment=value)
        if "anti_mention_spam" not in self.protections_data or not self.protections_data["anti_mention_spam"]:
            self.protections_data["anti_mention_spam"] = {'enabled': False, 'log_channel_id': None, 'punishment': value, 'timeout_duration': '1h'}
        else:
            self.protections_data["anti_mention_spam"]['punishment'] = value
        self._build_items()
        embed = await build_mention_targets_embed(self.guild_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    async def timeout_duration_callback(self, interaction: discord.Interaction):
        value = interaction.data['values'][0]
        await set_protection(self.guild_id, "anti_mention_spam", timeout_duration=value)
        if self.protections_data.get("anti_mention_spam"):
            self.protections_data["anti_mention_spam"]['timeout_duration'] = value
        self._build_items()
        embed = await build_mention_targets_embed(self.guild_id, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    async def add_target(self, interaction: discord.Interaction):
        modal = MentionSpamAddTargetModal(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.send_modal(modal)

    async def remove_target(self, interaction: discord.Interaction):
        if not pool:
            await interaction.response.send_message("Base de données indisponible.", ephemeral=True)
            return
        rows = await pool.fetch(
            "SELECT user_id FROM mention_spam_targets WHERE guild_id = $1 ORDER BY added_at DESC",
            str(self.guild_id)
        )
        if not rows:
            await interaction.response.send_message("Aucune cible à retirer.", ephemeral=True)
            return
        options = []
        for row in rows[:25]:
            uid = row['user_id']
            member = interaction.guild.get_member(int(uid))
            label = str(member) if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid))
        view = MentionSpamRemoveSelect(self.guild_id, self.owner_id, self.protections_data, self.page, options, interaction.guild)
        await interaction.response.edit_message(view=view)

    async def back(self, interaction: discord.Interaction):
        mod = next((m for m in PROTECTION_MODULES if m['key'] == "anti_mention_spam"), None)
        prot = self.protections_data.get("anti_mention_spam")
        embed = build_protection_detail_embed(mod, prot, interaction.guild)
        detail_view = ProtectionDetailView(self.guild_id, self.owner_id, "anti_mention_spam", self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=detail_view)


class MentionSpamAddTargetModal(discord.ui.Modal, title="Ajouter une cible mentions"):
    user_id_input = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Ex: 123456789012345678",
        required=True,
        min_length=17,
        max_length=20
    )

    def __init__(self, guild_id, owner_id, protections_data, page):
        super().__init__()
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page

    async def on_submit(self, interaction: discord.Interaction):
        user_id_str = self.user_id_input.value.strip()
        try:
            uid = int(user_id_str)
        except ValueError:
            await interaction.response.send_message("ID invalide. Entrez un ID numérique.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                await interaction.response.send_message("Membre introuvable sur ce serveur.", ephemeral=True)
                return

        if pool:
            existing = await pool.fetchrow(
                "SELECT id FROM mention_spam_targets WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), str(uid)
            )
            if existing:
                await interaction.response.send_message(f"{member.mention} est déjà dans les cibles.", ephemeral=True)
                return

            await pool.execute(
                "INSERT INTO mention_spam_targets (guild_id, user_id, added_by) VALUES ($1, $2, $3)",
                str(self.guild_id), str(uid), str(interaction.user.id)
            )
            await log_to_db('info', f'{interaction.user} added {member} to mention spam targets in {interaction.guild.name}')

        embed = await build_mention_targets_embed(self.guild_id, interaction.guild)
        view = MentionSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=view)


class MentionSpamRemoveSelect(discord.ui.View):
    def __init__(self, guild_id, owner_id, protections_data, page, options, guild):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protections_data = protections_data
        self.page = page
        self.guild = guild
        self.select = discord.ui.Select(placeholder="Choisir une cible à retirer...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Vous n'êtes pas autorisé.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        uid = self.select.values[0]
        if pool:
            await pool.execute(
                "DELETE FROM mention_spam_targets WHERE guild_id = $1 AND user_id = $2",
                str(self.guild_id), uid
            )
            await log_to_db('info', f'{interaction.user} removed <@{uid}> from mention spam targets in {interaction.guild.name}')

        embed = await build_mention_targets_embed(self.guild_id, interaction.guild)
        view = MentionSpamTargetsView(self.guild_id, self.owner_id, self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=view)


class LogChannelModal(discord.ui.Modal, title="Configurer le salon de logs"):
    channel_id_input = discord.ui.TextInput(
        label="ID du salon de logs",
        placeholder="Ex: 1245008221731557478 (vide pour retirer)",
        required=False,
        max_length=20
    )

    def __init__(self, guild_id, owner_id, protection_key, protections_data, page):
        super().__init__()
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.protection_key = protection_key
        self.protections_data = protections_data
        self.page = page

    async def on_submit(self, interaction: discord.Interaction):
        channel_id_str = self.channel_id_input.value.strip()
        if channel_id_str:
            try:
                cid = int(channel_id_str)
                channel = interaction.guild.get_channel(cid)
                if not channel:
                    await interaction.response.send_message("Salon introuvable sur ce serveur.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("ID invalide.", ephemeral=True)
                return
            await set_protection(self.guild_id, self.protection_key, log_channel_id=channel_id_str)
            if self.protection_key not in self.protections_data or not self.protections_data[self.protection_key]:
                self.protections_data[self.protection_key] = {'enabled': False, 'log_channel_id': channel_id_str, 'punishment': 'ban'}
            else:
                self.protections_data[self.protection_key]['log_channel_id'] = channel_id_str
        else:
            await set_protection(self.guild_id, self.protection_key, log_channel_id="")
            if self.protections_data.get(self.protection_key):
                self.protections_data[self.protection_key]['log_channel_id'] = None

        mod = next((m for m in PROTECTION_MODULES if m['key'] == self.protection_key), None)
        embed = build_protection_detail_embed(mod, self.protections_data.get(self.protection_key), interaction.guild)
        detail_view = ProtectionDetailView(self.guild_id, self.owner_id, self.protection_key, self.protections_data, self.page)
        await interaction.response.edit_message(embed=embed, view=detail_view)
        await log_to_db('info', f'{interaction.user} configured log channel for {mod["label"]} in {interaction.guild.name}')


async def load_all_protections(guild_id):
    data = {}
    if pool:
        rows = await pool.fetch(
            "SELECT * FROM guild_protections WHERE guild_id = $1",
            str(guild_id)
        )
        for row in rows:
            data[row['protection_key']] = {
                'enabled': row['enabled'],
                'log_channel_id': row['log_channel_id'],
                'punishment': row['punishment'],
                'timeout_duration': row.get('timeout_duration', '1h'),
                'whitelist_bypass': row.get('whitelist_bypass', False)
            }
    return data


@bot.tree.command(name="panel", description="Gérer les modules de protection du serveur.")
@app_commands.default_permissions(administrator=True)
async def panel_command(interaction: discord.Interaction):
    try:
        is_allowed = await is_owner_or_ownerlist(interaction.guild, interaction.user.id)
        if not is_allowed:
            await interaction.response.send_message("Vous n'êtes pas autorisé à utiliser cette commande.", ephemeral=True)
            return

        protections_data = await load_all_protections(interaction.guild.id)
        total_pages = (len(PROTECTION_MODULES) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        embed = build_panel_page_embed(protections_data, 0, total_pages)
        view = PanelView(interaction.guild.id, interaction.user.id, protections_data, 0)
        await interaction.response.send_message(embed=embed, view=view)
        await log_to_db('info', f'/panel used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /panel command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /panel: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="logs", description="Créer le salon logs・général pour tous les événements du serveur.")
@app_commands.default_permissions(administrator=True)
async def logs_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return
        if not await is_bot_owner_or_server_owner(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Seul le propriétaire du bot ou le créateur du serveur peut utiliser cette commande.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for role in guild.roles:
            if role.permissions.administrator and role != guild.default_role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)

        view_only_role = guild.get_role(1521524197942231141)
        if view_only_role:
            overwrites[view_only_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                manage_messages=False,
                manage_channels=False,
            )

        category = discord.utils.get(guild.categories, name="Logs - Général")
        if not category:
            category = await guild.create_category("Logs - Général", overwrites=overwrites)
            try:
                await category.edit(position=len(guild.categories))
            except Exception:
                pass
        else:
            await category.edit(overwrites=overwrites)

        topics = {
            "role":    "Logs des rôles (création, suppression, modification, position).",
            "channel": "Logs des salons et fils (création, suppression, modification, webhooks).",
            "member":  "Logs des membres (join, leave, ban, kick, timeout, ajout de bot).",
            "voice":   "Logs vocaux (connexion, déplacement, mute, sourdine, déconnexion).",
            "message": "Logs des messages (suppression, édition, anti-spam, anti-lien, toxicité).",
            "server":  "Logs serveur (paramètres, emojis, stickers, invitations).",
        }

        created = {}
        for cat_key, ch_name in LOG_CHANNELS.items():
            existing = discord.utils.get(category.text_channels, name=ch_name)
            if not existing:
                existing = await guild.create_text_channel(
                    ch_name,
                    category=category,
                    overwrites=overwrites,
                    topic=topics.get(cat_key, "Logs du serveur."),
                )
            created[cat_key] = existing

        for module in PROTECTION_MODULES:
            cat_key = PROTECTION_TO_CATEGORY.get(module['key'], "server")
            target_ch = created.get(cat_key) or created.get("server")
            if target_ch:
                await set_protection(str(guild.id), module['key'], log_channel_id=str(target_ch.id))

        embed = discord.Embed(
            title="✅ Logs configurés",
            description=(
                "Les salons de logs ont été créés/configurés dans la catégorie **Logs - Général** :\n\n"
                f"> 🎭 Rôles → {created['role'].mention}\n"
                f"> 📁 Salons & threads → {created['channel'].mention}\n"
                f"> 👤 Membres → {created['member'].mention}\n"
                f"> 🔊 Vocal → {created['voice'].mention}\n"
                f"> 📝 Messages → {created['message'].mention}\n"
                f"> ⚙️ Serveur (paramètres, emojis, invitations) → {created['server'].mention}\n\n"
                "Chaque module de protection est désormais routé vers le bon salon."
            ),
            color=0x2b2d31
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await log_to_db('info', f'/logs used by {interaction.user} in {guild.name}')
    except Exception as e:
        logger.error(f"Error in /logs command: {traceback.format_exc()}")
        try:
            await interaction.followup.send("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="ticketslogs", description="Créer le salon de logs des tickets.")
@app_commands.default_permissions(administrator=True)
async def ticketslogs_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return
        if not await is_bot_owner_or_server_owner(interaction.guild, interaction.user.id):
            await interaction.response.send_message("Seul le propriétaire du bot ou le créateur du serveur peut utiliser cette commande.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True),
        }
        for role in guild.roles:
            if role.permissions.administrator and role != guild.default_role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)

        view_only_role = guild.get_role(1521524197942231141)
        if view_only_role:
            overwrites[view_only_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                manage_messages=False,
                manage_channels=False,
            )

        category = discord.utils.get(guild.categories, name="Orizon - Logs")
        if not category:
            category = await guild.create_category("Orizon - Logs", overwrites=overwrites)
            try:
                await category.edit(position=len(guild.categories))
            except Exception:
                pass

        existing = discord.utils.get(category.text_channels, name=TICKET_LOG_CHANNEL)
        if not existing:
            log_ch = await guild.create_text_channel(
                TICKET_LOG_CHANNEL,
                category=category,
                overwrites=overwrites,
                topic="Historique complet des tickets (ouverture, prise en charge, fermeture)."
            )
        else:
            log_ch = existing
            try:
                await log_ch.edit(category=category, overwrites=overwrites)
            except Exception:
                pass

        await set_guild_setting(guild.id, 'ticket_log_channel_id', log_ch.id)

        embed = discord.Embed(
            title="✅ Logs tickets configurés",
            description=(
                f"Le salon {log_ch.mention} recevra désormais tous les événements de tickets :\n"
                f"> 📨 Ouverture d'un ticket\n"
                f"> 🎫 Prise en charge (claim)\n"
                f"> 🔒 Fermeture\n"
            ),
            color=0x2b2d31,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await log_to_db('info', f'/ticketslogs used by {interaction.user} in {guild.name} -> #{log_ch.name}')
    except Exception as e:
        logger.error(f"Error in /ticketslogs command: {traceback.format_exc()}")
        try:
            await interaction.followup.send("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="help", description="Afficher la liste des commandes du bot.")
@app_commands.default_permissions(administrator=True)
async def help_command(interaction: discord.Interaction):
    try:
        cmd_ids = await get_command_ids(interaction.guild) if interaction.guild else {}
        embed = build_help_embed(cmd_ids)
        await interaction.response.send_message(embed=embed)
        await log_to_db('info', f'/help used by {interaction.user}')
    except Exception as e:
        logger.error(f"Error in /help command: {traceback.format_exc()}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


TICKET_TYPES = {
    "bda": {"label": "Besoin d'aide (BDA)", "short": "bda", "emoji": "🆘", "category_key": "moderation"},
    "bug": {"label": "Report de Bug", "short": "bug", "emoji": "🐞", "category_key": "moderation"},
    "amj": {"label": "Demande pour l'Animation/Maître du Jeux", "short": "amj", "emoji": "🎲", "category_key": "animation"},
    "ps": {"label": "Plainte Staff", "short": "ps", "emoji": "📣", "category_key": "administration"},
    "ddb": {"label": "Demande de déban", "short": "ddb", "emoji": "🔓", "category_key": "administration"},
    "rpk": {"label": "Demande de RPK", "short": "rpk", "emoji": "⚔️", "category_key": "gerance"},
    "grp": {"label": "Ticket Gérant RP", "short": "grp", "emoji": "🎭", "category_key": "gerance"},
    "eg": {"label": "Entretien avec la Gérance", "short": "eg", "emoji": "💼", "category_key": "gerance"},
    "autre": {"label": "Autres", "short": "autre", "emoji": "❓", "category_key": "gerance"},
    "rb": {"label": "Demande de Remboursement", "short": "rb", "emoji": "💸", "category_key": "direction"},
    "pbq": {"label": "Problème avec la boutique", "short": "pbq", "emoji": "🛒", "category_key": "direction"},
    "ed": {"label": "Entretien avec la Direction", "short": "ed", "emoji": "🏛️", "category_key": "direction"},
}

TICKET_ORDER = ["bda", "rb", "rpk", "grp", "pbq", "ddb", "ps", "bug", "amj", "ed", "eg", "autre"]

ROLE_ADMIN = 1500214818936848534
ROLE_GERANCE = 1500213826107343039
ROLE_RESP_MOD = 1500216689093251174
ROLE_RESP_ANIM = 1500216707883597854

CATEGORY_CONFIG = {
    "moderation": {
        "primary_role": 1500212869243998239,
        "category_id": 1500213318491705455,
        "extra_view_roles": [ROLE_ADMIN, ROLE_RESP_MOD, ROLE_GERANCE],
        "auto_create": False,
        "open_to_primary_role": False,
    },
    "animation": {
        "primary_role": 1500216204747608164,
        "category_id": 1500216301535363172,
        "extra_view_roles": [ROLE_ADMIN, ROLE_RESP_ANIM, ROLE_GERANCE],
        "auto_create": False,
        "open_to_primary_role": False,
    },
    "administration": {
        "primary_role": ROLE_ADMIN,
        "category_id": 1500213926502338560,
        "extra_view_roles": [ROLE_GERANCE],
        "auto_create": False,
        "open_to_primary_role": True,
    },
    "gerance": {
        "primary_role": ROLE_GERANCE,
        "category_id": 1500213926502338560,
        "extra_view_roles": [],
        "auto_create": False,
        "open_to_primary_role": True,
    },
    "direction": {
        "primary_role": 1500215064177934507,
        "category_id": 1500215169999966248,
        "extra_view_roles": [],
        "auto_create": True,
        "open_to_primary_role": False,
    },
}


def make_short_name(member):
    raw = (getattr(member, 'display_name', None) or getattr(member, 'name', None) or str(member.id))
    name = raw.lower()
    name = re.sub(r'[^a-z0-9]', '', name)
    return name[:10] or str(member.id)[-4:]


class TicketPanelSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for k in TICKET_ORDER:
            t = TICKET_TYPES[k]
            options.append(discord.SelectOption(
                label=t["label"][:100],
                value=k,
                emoji=t.get("emoji"),
            ))
        super().__init__(
            placeholder="Choisissez la raison de votre ticket",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_panel_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await handle_ticket_creation(interaction, self.values[0])


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())


class TicketPanelLayout(discord.ui.LayoutView):
    def __init__(self, with_banner: bool = True):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=0x2d1532)

        if with_banner:
            try:
                gallery = discord.ui.MediaGallery()
                gallery.add_item(media="https://i.imgur.com/wrCe899.png")
                container.add_item(gallery)
            except Exception:
                pass

        container.add_item(discord.ui.TextDisplay(
            "## 🧊  OUVRIR UN TICKET AUPRÈS DU STAFF"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "### COMMENT ÇA MARCHE ?\n"
            "1️⃣  Sélectionnez votre raison dans le menu ci-dessous.\n"
            "2️⃣  Une demande sera envoyée au staff.\n"
            "3️⃣  Vous recevrez un MP quand votre ticket sera accepté."
        ))
        container.add_item(discord.ui.TextDisplay(
            "### RÈGLES DE COURTOISIE\n"
            "• Merci de rester poli et respectueux.\n"
            "• Toute forme de harcèlement est interdite."
        ))
        container.add_item(discord.ui.TextDisplay(
            "### INFORMATION IMPORTANTE\n"
            "» Pour tout ticket de demande d'entretien, problèmes boutiques ou autorisation RP, "
            "merci de faire preuve de patience. Les délais de réponses peuvent être plus ou moins longs."
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "### Sélection du ticket\n"
            "Choisissez une raison dans le menu ci-dessous."
        ))

        action_row = discord.ui.ActionRow()
        action_row.add_item(TicketPanelSelect())
        container.add_item(action_row)

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# Orizon • Poudlard | Tickets"
        ))

        self.add_item(container)


class ClaimTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r'claim_ticket:(?P<id>\d+)'):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(
            discord.ui.Button(
                label='Claim',
                emoji='🎫',
                style=discord.ButtonStyle.green,
                custom_id=f'claim_ticket:{ticket_id}',
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['id']))

    async def callback(self, interaction: discord.Interaction):
        await handle_claim(interaction, self.ticket_id)


class CloseTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r'close_ticket:(?P<id>\d+)'):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id
        super().__init__(
            discord.ui.Button(
                label='Fermer le ticket',
                emoji='🔒',
                style=discord.ButtonStyle.danger,
                custom_id=f'close_ticket:{ticket_id}',
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['id']))

    async def callback(self, interaction: discord.Interaction):
        await handle_close_ticket(interaction, self.ticket_id)


async def handle_close_ticket(interaction: discord.Interaction, ticket_id: int):
    try:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        if not guild or not pool:
            await interaction.followup.send("❌ Erreur de configuration.", ephemeral=True)
            return

        ticket = await pool.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        if not ticket:
            await interaction.followup.send("❌ Ticket introuvable.", ephemeral=True)
            return

        if str(member.id) == ticket['user_id']:
            await interaction.followup.send(
                "❌ Vous ne pouvez pas fermer votre propre ticket. Demandez à un membre du staff.",
                ephemeral=True,
            )
            return

        ticket_info = TICKET_TYPES.get(ticket['ticket_type'])
        if not ticket_info:
            await interaction.followup.send("❌ Type de ticket invalide.", ephemeral=True)
            return

        config = CATEGORY_CONFIG[ticket_info['category_key']]
        allowed_role_ids = {ROLE_ADMIN, ROLE_GERANCE}
        allowed_role_ids.add(config['primary_role'])
        for r in config.get('extra_view_roles', []):
            allowed_role_ids.add(r)

        member_role_ids = {r.id for r in getattr(member, 'roles', [])}
        if not (allowed_role_ids & member_role_ids):
            await interaction.followup.send(
                "❌ Vous n'avez pas la permission de fermer ce ticket.",
                ephemeral=True,
            )
            return

        closed_row = await pool.fetchrow(
            "UPDATE tickets SET status = 'closed', closed_by = $1, closed_at = NOW() "
            "WHERE id = $2 AND status <> 'closed' RETURNING id",
            str(member.id), ticket_id,
        )
        if not closed_row:
            await interaction.followup.send("❌ Ce ticket est déjà en cours de fermeture.", ephemeral=True)
            return

        channel = interaction.channel
        if channel is not None:
            channel_name = getattr(channel, "name", None) or str(getattr(channel, "id", "inconnu"))
        else:
            channel_name = f"channel-{ticket['channel_id'] or 'inconnu'}"

        creator_mention = f"<@{ticket['user_id']}>"
        claimer_mention = f"<@{ticket['claimer_id']}>" if ticket['claimer_id'] else "—"
        created_at = ticket['created_at']
        duration_str = "—"
        try:
            if created_at:
                delta = datetime.datetime.utcnow() - created_at.replace(tzinfo=None)
                total = int(delta.total_seconds())
                h, rem = divmod(total, 3600)
                m, s = divmod(rem, 60)
                duration_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
        except Exception:
            pass

        log_embed = discord.Embed(
            title="🔒 Ticket fermé",
            color=0xed4245,
            timestamp=datetime.datetime.utcnow(),
        )
        log_embed.add_field(name="ID Ticket", value=f"`#{ticket_id}`", inline=True)
        log_embed.add_field(name="Type", value=ticket_info['label'], inline=True)
        log_embed.add_field(name="Salon", value=f"`#{channel_name}`", inline=True)
        log_embed.add_field(name="Demandeur", value=creator_mention, inline=True)
        log_embed.add_field(name="Pris en charge par", value=claimer_mention, inline=True)
        log_embed.add_field(name="Fermé par", value=member.mention, inline=True)
        log_embed.add_field(name="Ouvert le", value=(created_at.strftime("%d/%m/%Y %H:%M UTC") if created_at else "—"), inline=True)
        log_embed.add_field(name="Durée", value=duration_str, inline=True)
        await send_ticket_log(interaction.guild, log_embed)

        try:
            close_embed = discord.Embed(
                title="🔒 Ticket fermé",
                description=f"Fermé par {member.mention}. Le salon sera supprimé dans 5 secondes.",
                color=0xed4245,
                timestamp=datetime.datetime.utcnow(),
            )
            await channel.send(embed=close_embed)
        except Exception:
            pass

        try:
            await interaction.followup.send("✅ Fermeture du ticket en cours...", ephemeral=True)
        except Exception:
            pass

        await log_to_db('info', f'Ticket #{ticket_id} closed by {member} -> #{channel_name}')

        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket #{ticket_id} fermé par {member}")
        except Exception as e:
            logger.error(f"Failed to delete ticket channel #{ticket_id}: {e}")

    except Exception as e:
        logger.error(f"Error in handle_close_ticket: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in handle_close_ticket: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


async def handle_ticket_creation(interaction: discord.Interaction, ticket_type_key: str):
    try:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        if not guild:
            await interaction.followup.send("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return

        ticket_info = TICKET_TYPES.get(ticket_type_key)
        if not ticket_info:
            await interaction.followup.send("❌ Type de ticket inconnu.", ephemeral=True)
            return
        config = CATEGORY_CONFIG[ticket_info["category_key"]]

        if not pool:
            await interaction.followup.send("❌ Base de données indisponible.", ephemeral=True)
            return

        existing = await pool.fetchrow(
            "SELECT id, status, channel_id FROM tickets WHERE guild_id = $1 AND user_id = $2 AND ticket_type = $3 AND status IN ('pending', 'open')",
            str(guild.id), str(user.id), ticket_type_key
        )
        if existing:
            if existing['status'] == 'pending':
                await interaction.followup.send(
                    f"❌ Vous avez déjà une demande de **{ticket_info['label']}** en attente. Veuillez patienter qu'un membre du staff la prenne en charge.",
                    ephemeral=True
                )
            else:
                ch_mention = f"<#{existing['channel_id']}>" if existing['channel_id'] else "introuvable"
                await interaction.followup.send(
                    f"❌ Vous avez déjà un ticket **{ticket_info['label']}** ouvert : {ch_mention}",
                    ephemeral=True
                )
            return

        ticket_id = await pool.fetchval(
            "INSERT INTO tickets (guild_id, user_id, ticket_type, status) VALUES ($1, $2, $3, 'pending') RETURNING id",
            str(guild.id), str(user.id), ticket_type_key
        )

        if config["auto_create"]:
            channel = await create_ticket_channel(guild, user, ticket_info, config, ticket_id, claimer=None)
            if not channel:
                await pool.execute("UPDATE tickets SET status = 'closed' WHERE id = $1", ticket_id)
                await interaction.followup.send("❌ Impossible de créer le salon de ticket. Vérifiez la configuration de la catégorie.", ephemeral=True)
                return
            await pool.execute(
                "UPDATE tickets SET channel_id = $1 WHERE id = $2",
                str(channel.id), ticket_id
            )
            await interaction.followup.send(
                f"✅ Votre ticket a été créé : {channel.mention}\nUn membre de la direction viendra vous répondre.",
                ephemeral=True
            )
            await log_to_db('info', f'Ticket #{ticket_id} ({ticket_info["label"]}) auto-created by {user} in {guild.name}')

            log_embed = discord.Embed(
                title="📨 Ticket ouvert",
                color=0x3498db,
                timestamp=datetime.datetime.utcnow(),
            )
            log_embed.add_field(name="ID Ticket", value=f"`#{ticket_id}`", inline=True)
            log_embed.add_field(name="Type", value=ticket_info['label'], inline=True)
            log_embed.add_field(name="Salon", value=channel.mention, inline=True)
            log_embed.add_field(name="Demandeur", value=f"{user.mention} (`{user}`)", inline=False)
            log_embed.add_field(name="Création", value="Automatique (catégorie direction)", inline=False)
            try:
                log_embed.set_thumbnail(url=user.display_avatar.url)
            except Exception:
                pass
            await send_ticket_log(guild, log_embed)
        else:
            primary_role = guild.get_role(config["primary_role"])
            if not primary_role:
                await pool.execute("UPDATE tickets SET status = 'closed' WHERE id = $1", ticket_id)
                await interaction.followup.send("❌ Le rôle de modération est introuvable. Contactez un administrateur.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📨 Nouveau ticket : {ticket_info['label']}",
                description=(
                    f"**Demandeur :** {user.mention} (`{user}`)\n"
                    f"**Serveur :** {guild.name}\n"
                    f"**Type :** {ticket_info['label']}\n"
                    f"**ID Ticket :** `{ticket_id}`"
                ),
                color=0x2b2d31,
                timestamp=datetime.datetime.utcnow()
            )
            try:
                embed.set_thumbnail(url=user.display_avatar.url)
            except Exception:
                pass
            embed.set_footer(text="Cliquez sur 'Claim' pour prendre en charge ce ticket.")

            view = discord.ui.View(timeout=None)
            view.add_item(ClaimTicketButton(ticket_id))

            log_embed = discord.Embed(
                title="📨 Ticket ouvert",
                color=0x3498db,
                timestamp=datetime.datetime.utcnow(),
            )
            log_embed.add_field(name="ID Ticket", value=f"`#{ticket_id}`", inline=True)
            log_embed.add_field(name="Type", value=ticket_info['label'], inline=True)
            log_embed.add_field(name="Statut", value="En attente de claim", inline=True)
            log_embed.add_field(name="Demandeur", value=f"{user.mention} (`{user}`)", inline=False)
            log_embed.add_field(name="Rôle notifié", value=primary_role.mention, inline=False)
            try:
                log_embed.set_thumbnail(url=user.display_avatar.url)
            except Exception:
                pass
            await send_ticket_log(guild, log_embed)

            sent = 0
            failed = 0
            for member in primary_role.members:
                if member.bot:
                    continue
                try:
                    await member.send(embed=embed, view=view)
                    sent += 1
                except Exception:
                    failed += 1

            await interaction.followup.send(
                f"✅ Votre demande de **{ticket_info['label']}** a bien été envoyée à l'équipe ({sent} membre(s) notifié(s)). Vous recevrez un MP dès qu'un membre du staff la prendra en charge.",
                ephemeral=True
            )
            await log_to_db('info', f'Ticket #{ticket_id} ({ticket_info["label"]}) requested by {user} in {guild.name}, DMed to {sent} mods (failed: {failed})')
    except Exception as e:
        logger.error(f"Error in handle_ticket_creation: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in handle_ticket_creation: {e}')
        except Exception:
            pass
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


async def handle_claim(interaction: discord.Interaction, ticket_id: int):
    try:
        await interaction.response.defer(ephemeral=True)
        if not pool:
            await interaction.followup.send("❌ Base de données indisponible.", ephemeral=True)
            return

        ticket = await pool.fetchrow("SELECT * FROM tickets WHERE id = $1", ticket_id)
        if not ticket:
            await interaction.followup.send("❌ Ticket introuvable.", ephemeral=True)
            return

        if ticket['status'] == 'open' and ticket['claimer_id']:
            await interaction.followup.send(f"❌ Ce ticket a déjà été pris en charge par <@{ticket['claimer_id']}>.", ephemeral=True)
            return
        if ticket['status'] == 'closed':
            await interaction.followup.send("❌ Ce ticket est fermé.", ephemeral=True)
            return

        guild = bot.get_guild(int(ticket['guild_id']))
        if not guild:
            await interaction.followup.send("❌ Serveur introuvable.", ephemeral=True)
            return

        ticket_info = TICKET_TYPES.get(ticket['ticket_type'])
        if not ticket_info:
            await interaction.followup.send("❌ Type de ticket inconnu.", ephemeral=True)
            return
        config = CATEGORY_CONFIG[ticket_info['category_key']]

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                await interaction.followup.send("❌ Vous n'êtes pas membre du serveur.", ephemeral=True)
                return

        primary_role = guild.get_role(config['primary_role'])
        if not primary_role or primary_role not in member.roles:
            await interaction.followup.send("❌ Vous n'avez pas le rôle requis pour prendre ce ticket en charge.", ephemeral=True)
            return

        creator = guild.get_member(int(ticket['user_id']))
        if not creator:
            try:
                creator = await guild.fetch_member(int(ticket['user_id']))
            except Exception:
                await interaction.followup.send("❌ L'utilisateur ayant ouvert le ticket n'est plus sur le serveur.", ephemeral=True)
                return

        claimed_row = await pool.fetchrow(
            """
            UPDATE tickets
               SET status = 'open',
                   claimer_id = $1,
                   claimed_at = NOW()
             WHERE id = $2
               AND status = 'pending'
               AND claimer_id IS NULL
            RETURNING id
            """,
            str(member.id), ticket_id
        )
        if not claimed_row:
            current = await pool.fetchrow("SELECT status, claimer_id FROM tickets WHERE id = $1", ticket_id)
            if current and current['claimer_id']:
                await interaction.followup.send(f"❌ Ce ticket vient d'être pris en charge par <@{current['claimer_id']}>.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Ce ticket n'est plus disponible.", ephemeral=True)
            return

        if ticket['channel_id']:
            channel = guild.get_channel(int(ticket['channel_id']))
            if not channel:
                await pool.execute(
                    "UPDATE tickets SET status = 'pending', claimer_id = NULL, claimed_at = NULL WHERE id = $1",
                    ticket_id
                )
                await interaction.followup.send("❌ Salon du ticket introuvable.", ephemeral=True)
                return

            creator_short = make_short_name(creator)
            claimer_short = make_short_name(member)
            new_name = f"{ticket_info['emoji']}{ticket_info['short']}-{creator_short}-{claimer_short}"

            new_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
                creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True),
            }
            for role_id in config.get("extra_view_roles", []):
                role = guild.get_role(role_id)
                if role:
                    new_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

            try:
                await channel.edit(name=new_name, overwrites=new_overwrites, reason=f"Ticket #{ticket_id} pris en charge par {member}")
            except Exception as e:
                logger.error(f"Failed to rename/restrict ticket channel: {e}")

            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

            try:
                claim_embed = discord.Embed(
                    description=f"✅ Ticket pris en charge par {member.mention}.",
                    color=0x2ecc71,
                    timestamp=datetime.datetime.utcnow()
                )
                await channel.send(embed=claim_embed)
            except Exception:
                pass

            await interaction.followup.send(f"✅ Ticket pris en charge : {channel.mention}", ephemeral=True)
            await log_to_db('info', f'Ticket #{ticket_id} claimed (in-channel) by {member} -> {channel.name}')

            log_embed = discord.Embed(
                title="🎫 Ticket pris en charge",
                color=0x2ecc71,
                timestamp=datetime.datetime.utcnow(),
            )
            log_embed.add_field(name="ID Ticket", value=f"`#{ticket_id}`", inline=True)
            log_embed.add_field(name="Type", value=ticket_info['label'], inline=True)
            log_embed.add_field(name="Salon", value=channel.mention, inline=True)
            log_embed.add_field(name="Demandeur", value=creator.mention, inline=True)
            log_embed.add_field(name="Pris par", value=member.mention, inline=True)
            await send_ticket_log(guild, log_embed)
        else:
            channel = await create_ticket_channel(guild, creator, ticket_info, config, ticket_id, claimer=member)
            if not channel:
                await pool.execute(
                    "UPDATE tickets SET status = 'pending', claimer_id = NULL, claimed_at = NULL WHERE id = $1",
                    ticket_id
                )
                await interaction.followup.send("❌ Impossible de créer le salon. Vérifiez la configuration de la catégorie.", ephemeral=True)
                return

            await pool.execute(
                "UPDATE tickets SET channel_id = $1 WHERE id = $2",
                str(channel.id), ticket_id
            )

            try:
                claimed_embed = discord.Embed(
                    title="✅ Ticket pris en charge",
                    description=(
                        f"**Type :** {ticket_info['label']}\n"
                        f"**Pris par :** {member.mention}\n"
                        f"**Salon :** {channel.mention}"
                    ),
                    color=0x2ecc71,
                    timestamp=datetime.datetime.utcnow()
                )
                await interaction.message.edit(embed=claimed_embed, view=None)
            except Exception:
                pass

            await interaction.followup.send(f"✅ Ticket pris en charge : {channel.mention}", ephemeral=True)
            await log_to_db('info', f'Ticket #{ticket_id} claimed by {member} -> {channel.name}')

            log_embed = discord.Embed(
                title="🎫 Ticket pris en charge",
                color=0x2ecc71,
                timestamp=datetime.datetime.utcnow(),
            )
            log_embed.add_field(name="ID Ticket", value=f"`#{ticket_id}`", inline=True)
            log_embed.add_field(name="Type", value=ticket_info['label'], inline=True)
            log_embed.add_field(name="Salon", value=channel.mention, inline=True)
            log_embed.add_field(name="Demandeur", value=creator.mention, inline=True)
            log_embed.add_field(name="Pris par", value=member.mention, inline=True)
            await send_ticket_log(guild, log_embed)
    except Exception as e:
        logger.error(f"Error in handle_claim: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in handle_claim: {e}')
        except Exception:
            pass
        try:
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


async def create_ticket_channel(guild, creator, ticket_info, config, ticket_id, claimer=None):
    category = guild.get_channel(config['category_id'])
    if not category or not isinstance(category, discord.CategoryChannel):
        logger.error(f"Category {config['category_id']} not found in {guild.name}")
        return None

    creator_short = make_short_name(creator)
    if claimer:
        claimer_short = make_short_name(claimer)
        channel_name = f"{ticket_info['emoji']}{ticket_info['short']}-{creator_short}-{claimer_short}"
    else:
        channel_name = f"{ticket_info['emoji']}{ticket_info['short']}-{creator_short}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
        creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
    }

    if claimer:
        overwrites[claimer] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True, manage_messages=True)

    if config.get("auto_create") and not claimer:
        primary_role = guild.get_role(config['primary_role'])
        if primary_role:
            overwrites[primary_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

    if config.get("open_to_primary_role"):
        primary_role = guild.get_role(config['primary_role'])
        if primary_role:
            overwrites[primary_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

    for role_id in config.get("extra_view_roles", []):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_id} — {ticket_info['label']} — Ouvert par {creator}"
        )
    except Exception as e:
        logger.error(f"Failed to create ticket channel: {e}")
        return None

    welcome_lines = [
        f"**Ouvert par :** {creator.mention}",
        f"**Type :** {ticket_info['label']}",
        f"**ID Ticket :** `{ticket_id}`",
    ]
    if claimer:
        welcome_lines.append(f"**Pris en charge par :** {claimer.mention}")
    welcome_lines.append("")
    welcome_lines.append("Veuillez décrire votre demande en détail. Un membre du staff vous répondra dans les plus brefs délais.")

    welcome_embed = discord.Embed(
        title=f"🎫 Ticket — {ticket_info['label']}",
        description="\n".join(welcome_lines),
        color=0x2b2d31,
        timestamp=datetime.datetime.utcnow()
    )

    try:
        if claimer:
            view = discord.ui.View(timeout=None)
            view.add_item(CloseTicketButton(ticket_id))
            await channel.send(content=f"{creator.mention} {claimer.mention}", embed=welcome_embed, view=view)
        else:
            primary_role = guild.get_role(config['primary_role'])
            ping = primary_role.mention if primary_role else ""
            view = discord.ui.View(timeout=None)
            view.add_item(ClaimTicketButton(ticket_id))
            view.add_item(CloseTicketButton(ticket_id))
            await channel.send(content=f"{creator.mention} {ping}".strip(), embed=welcome_embed, view=view)
    except Exception as e:
        logger.error(f"Failed to send welcome message in ticket channel: {e}")

    try:
        dm_embed = discord.Embed(
            title="📩 Votre ticket a été pris en charge",
            description=(
                f"**Serveur :** {guild.name}\n"
                f"**Type :** {ticket_info['label']}\n"
                f"**Salon :** {channel.mention}"
                + (f"\n**Pris en charge par :** {claimer}" if claimer else "")
            ),
            color=0x2ecc71,
            timestamp=datetime.datetime.utcnow()
        )
        await creator.send(embed=dm_embed)
    except Exception:
        pass

    return channel


@bot.tree.command(name="ticket", description="Envoyer le panneau d'ouverture de tickets dans ce salon.")
@app_commands.default_permissions(administrator=True)
async def ticket_command(interaction: discord.Interaction):
    try:
        view = TicketPanelLayout()
        await interaction.channel.send(view=view)

        await interaction.response.send_message("✅ Panneau de tickets envoyé.", ephemeral=True)
        await log_to_db('info', f'/ticket panel sent by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /ticket command: {traceback.format_exc()}")
        try:
            await log_to_db('error', f'Error in /ticket: {e}')
        except Exception:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="contact", description="Envoyer le guide des contacts du serveur dans ce salon.")
@app_commands.default_permissions(administrator=True)
async def contact_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        CONTACT_ROLES = [
            1521498188148768809,
            1521523197764305130,
            1521523168659898573,
            1521523237383835718,
            1521523276315099309,
        ]

        def get_members_for_role(role_id):
            role = guild.get_role(role_id)
            if not role:
                return []
            return [m for m in role.members if not m.bot]

        important_lines = [
            "**Respectez notre équipe !**",
            "Avant de contacter un membre en direct, ouvrez d'abord un ticket pour obtenir une réponse plus rapide.",
            "• Il est fortement déconseillé de tenter de MP <@&1521498188148768809>.",
            "• Les membres de la Gérance (<@&1521523168659898573> et <@&1521523237383835718>) ne doivent pas être MP directement.",
            "• Privilégiez toujours le système des tickets.",
        ]

        important_block = "\n".join(important_lines)

        sep = "─" * 40

        equipe_lines = []
        for role_id in CONTACT_ROLES:
            role = guild.get_role(role_id)
            role_mention = f"<@&{role_id}>" if role else f"`Rôle inconnu ({role_id})`"
            members = get_members_for_role(role_id)
            equipe_lines.append(f"**–** {role_mention}")
            if members:
                for m in members:
                    equipe_lines.append(f"• {m.mention}")
            else:
                equipe_lines.append("• *Aucun membre disponible*")
            equipe_lines.append(sep)

        equipe_block = "\n".join(equipe_lines)

        description = (
            "# 🎲 GUIDE DES CONTACTS\n"
            "> Besoin d'aide ? Voici qui contacter selon votre demande.\n"
            "> Consultez les sections ci-dessous pour trouver le bon interlocuteur.\n\n"
            "# 🔰 IMPORTANT\n"
            f"{important_block}\n\n"
            "# 👥 ÉQUIPE DISPONIBLE\n\n"
            f"{equipe_block}\n"
            f"🔒 **Sujet sensible ou situation délicate ?**\n"
            f"Ouvrez un ticket via le système de tickets.\n"
            f"→ *Entretien avec la Direction*"
        )

        embed = discord.Embed(
            description=description,
            color=0x000000,
        )

        await interaction.channel.send(embed=embed)
        await interaction.followup.send("✅ Guide des contacts envoyé.", ephemeral=True)
        await log_to_db('info', f'/contact used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /contact command: {traceback.format_exc()}")
        try:
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="info", description="Envoyer le panel d'informations et des serveurs dans ce salon.")
@app_commands.default_permissions(administrator=True)
async def info_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        now = datetime.datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")

        description = (
            "# ⚙️ NOS SERVEURS DISCORD\n"
            "> Retrouvez l'ensemble de nos serveurs et liens clés.\n\n"

            "🌐 **SERVEURS DISCORDS GLOBAL**\n"
            "• [PoudlardRP](https://discord.gg/GbUYFXKuTn) • Serveur principal\n\n"

            "🎓 **SERVEURS MAISONS**\n"
            "• Serpentard\n"
            "• Gryffondor\n"
            "• Serdaigle\n"
            "• Poufsouffle\n\n"

            "🌍 **SERVEURS MONDE**\n"
            "• Monde Magique\n"
            "• Mage Indépendant\n\n"

            "🔗 **LIENS IMPORTANTS**\n"
            "• 🌐 **Portail Orizon Community** • Site internet\n"
            "• 💸 **Boutique** • Achats & soutiens\n"
            "• 📖 **Wiki PoudlardRP** • Documentation\n"
            "• 📋 **Règlement serveur (doc)** • Règles et procédures\n\n"

            "⚠️ **PRÉVENTION**\n"
            "• ⚠️ Les Discords joueurs ne sont pas affiliés à Orizon Poudlard.\n"
            "• ⚠️ Toute création de Discord sans autorisation peut entraîner des sanctions.\n\n"

            f"-# Orizon Poudlard • Liens Serveur | {now}"
        )

        embed = discord.Embed(
            description=description,
            color=0x000000,
        )

        await interaction.channel.send(embed=embed)
        await interaction.followup.send("✅ Panel d'informations envoyé.", ephemeral=True)
        await log_to_db('info', f'/info used by {interaction.user} in #{interaction.channel}')
    except Exception as e:
        logger.error(f"Error in /info command: {traceback.format_exc()}")
        try:
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="reception", description="Configurer ce salon comme salon de bienvenue automatique.")
async def reception_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _welcome_channel_cache[interaction.guild.id] = interaction.channel.id
        _save_config()
        try:
            await set_guild_setting(interaction.guild.id, 'welcome_channel_id', interaction.channel.id)
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ Salon de bienvenue configuré : {interaction.channel.mention}\n"
            "Le bot enverra automatiquement le message de bienvenue à chaque nouveau membre.",
            ephemeral=True
        )
        try:
            await log_to_db('info', f'/reception configured by {interaction.user} -> #{interaction.channel}')
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error in /reception command: {traceback.format_exc()}")
        try:
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


DISCORDUP_SEPARATOR = "▬▬▬▬▬▬▬▬"

DISCORDUP_STRUCTURE = [
    {
        "category": "【 🦍 】Important",
        "type": "text",
        "channels": [
            "🚦・consoles",
            "🪪・vos-rôles",
            "📙・règlement",
            "☑️・informations",
            "🧭・comment-rejoindre",
            "📞・qui-contacter",
            "🌐・reseaux-sociaux",
            "💎・boosters",
        ],
    },
    {
        "category": "【 🦍 】Actualité",
        "type": "text",
        "channels": [
            "📢・annonces-hrp",
            "🗞️・new-ninjarp",
            "🛠️・patch-notes",
            "🟦・trâme",
            "🎎・spoils",
            "✨・nouveauté",
            DISCORDUP_SEPARATOR,
            "🎁・giveaway",
            "🛒・boutique",
            DISCORDUP_SEPARATOR,
            "📋・recrutement-staff",
            "📱・tiktoker",
            "📊・sondages",
            "🔔・notif-stream",
        ],
    },
    {
        "category": "【 🦍 】Communauté HRP",
        "type": "text",
        "channels": [
            "🎀・stream",
            "👥・trombinoscope",
            "💬・discussion",
            "🎮・jeux",
            "🌐・commandes-bot",
            "🎨・artistes",
            "🖼️・edits",
        ],
    },
    {
        "category": "【 📖 】Communauté WL",
        "type": "text",
        "channels": [
            "🔹・infos-whitelist",
            "📢・events-rp",
            "🔥・events-hrp",
            DISCORDUP_SEPARATOR,
            "💬・discussion-wl",
            "📷・images-vidéos",
            DISCORDUP_SEPARATOR,
            "🌸・narration-rp",
            "👶・naissances-clans",
            "🩸・void-et-rpk",
            DISCORDUP_SEPARATOR,
            "🌷・tuto",
            "🤝・entraide",
            DISCORDUP_SEPARATOR,
            "💡・suggestions-wl",
            "💡・suggestions-rp-serveur",
            "💡・suggestions-créations",
        ],
    },
    {
        "category": "Report",
        "type": "text",
        "channels": [
            "⚡・suggestions-swep",
            "❎・report-bug",
            "❎・bug-jutsu",
            "❎・report-tenues",
        ],
    },
    {
        "category": "Salons Vocaux Communautaire",
        "type": "voice",
        "channels": [
            "🔨・général 1",
            "🌍・général 2",
            "🌙・général 3",
            "🎉・général 4",
            "💃・général 5",
        ],
    },
    {
        "category": "Staff",
        "type": "voice",
        "channels": [
            "⌛・attente staff",
        ],
    },
]


@bot.tree.command(name="discordup", description="Créer toute la structure de salons du serveur (Bot Owner uniquement).")
async def discordup_command(interaction: discord.Interaction):
    try:
        if interaction.guild is None:
            await interaction.response.send_message("❌ Cette commande doit être utilisée sur un serveur.", ephemeral=True)
            return
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Seul le propriétaire du bot peut utiliser cette commande.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        created_categories = 0
        created_channels = 0
        errors = []

        for block in DISCORDUP_STRUCTURE:
            cat_name = block["category"]
            ch_type = block["type"]
            try:
                category = discord.utils.get(guild.categories, name=cat_name)
                if not category:
                    category = await guild.create_category(cat_name)
                    created_categories += 1
            except Exception as e:
                errors.append(f"Catégorie « {cat_name} » : {e}")
                continue

            separator_seen = 0
            for ch_name in block["channels"]:
                try:
                    if ch_type == "voice":
                        existing = discord.utils.get(category.voice_channels, name=ch_name)
                        if not existing:
                            await guild.create_voice_channel(ch_name, category=category)
                            created_channels += 1
                    else:
                        if ch_name == DISCORDUP_SEPARATOR:
                            separator_seen += 1
                            existing_separators = sum(
                                1 for c in category.text_channels if c.name == DISCORDUP_SEPARATOR
                            )
                            if existing_separators < separator_seen:
                                await guild.create_text_channel(ch_name, category=category)
                                created_channels += 1
                        else:
                            existing = discord.utils.get(category.text_channels, name=ch_name)
                            if not existing:
                                await guild.create_text_channel(ch_name, category=category)
                                created_channels += 1
                except Exception as e:
                    errors.append(f"Salon « {ch_name} » : {e}")

        embed = discord.Embed(
            title="✅ Structure créée",
            description=(
                f"> 📂 Catégories créées : **{created_categories}**\n"
                f"> 📝 Salons créés : **{created_channels}**\n\n"
                + ("Aucune erreur." if not errors else "⚠️ Erreurs :\n" + "\n".join(f"• {e}" for e in errors[:10]))
            ),
            color=0x2b2d31
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await log_to_db('info', f'/discordup used by {interaction.user} in {guild.name} ({created_categories} cat, {created_channels} ch)')
    except Exception as e:
        logger.error(f"Error in /discordup command: {traceback.format_exc()}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Une erreur est survenue.", ephemeral=True)
            else:
                await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        except Exception:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"App command error: {error}\n{traceback.format_exc()}")
    try:
        await log_to_db('error', f'App command error: {error}')
    except Exception:
        pass
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("Une erreur est survenue.", ephemeral=True)
        else:
            await interaction.followup.send("Une erreur est survenue.", ephemeral=True)
    except Exception:
        pass


async def main():
    await init_db()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN is not set.")
        return
    logger.info("Starting bot...")
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
