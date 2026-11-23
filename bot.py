import os
import math
import random
import asyncio
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# レベルアップ通知を送る専用チャンネルのID（0 = 無効、発言チャンネルに送る）
LEVELUP_CHANNEL_ID = int(os.getenv("LEVELUP_CHANNEL_ID", "0"))

# XP 設定
XP_PER_MESSAGE_MIN = 15
XP_PER_MESSAGE_MAX = 25
XP_PER_THREAD = 50
XP_PER_REACTION = 3
XP_COOLDOWN_SECONDS = 60


# ---- レベル計算 ----

def xp_for_next_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    return sum(xp_for_next_level(i) for i in range(level))


def get_level(xp: int) -> int:
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


# ---- Bot ----

class LevelBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.xp_cooldowns: dict[tuple[int, int], datetime] = {}
        self.db_pool: asyncpg.Pool | None = None

    async def setup_hook(self):
        self.db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await self._init_db()
        await self.tree.sync()
        print("スラッシュコマンドを同期しました")

    async def _init_db(self):
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_xp (
                    guild_id BIGINT NOT NULL,
                    user_id  BIGINT NOT NULL,
                    xp       BIGINT DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

    async def close(self):
        if self.db_pool:
            await self.db_pool.close()
        await super().close()


bot = LevelBot()


# ---- DB ヘルパー ----

async def get_xp(guild_id: int, user_id: int) -> int:
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp FROM user_xp WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    return row["xp"] if row else 0


async def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, int]:
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT xp FROM user_xp WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        old_xp = row["xp"] if row else 0
        new_xp = old_xp + amount
        old_level = get_level(old_xp)
        new_level = get_level(new_xp)
        await conn.execute(
            """
            INSERT INTO user_xp (guild_id, user_id, xp) VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = EXCLUDED.xp
            """,
            guild_id, user_id, new_xp,
        )
    return old_level, new_level, new_xp


# ---- レベルアップ通知 ----

async def send_levelup(
    guild: discord.Guild | None,
    user: discord.Member | discord.User,
    level: int,
    total_xp: int,
    fallback: discord.abc.Messageable | None = None,
):
    channel: discord.abc.Messageable | None = None
    if LEVELUP_CHANNEL_ID and guild:
        channel = guild.get_channel(LEVELUP_CHANNEL_ID)
    if channel is None:
        channel = fallback
    if channel is None:
        return

    next_total = total_xp_for_level(level + 1)
    embed = discord.Embed(
        title="🎉 レベルアップ！",
        description=f"{user.mention} が **レベル {level}** になりました！",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="合計 XP", value=f"{total_xp:,} XP", inline=True)
    embed.add_field(name="次のレベルまで", value=f"{next_total - total_xp:,} XP", inline=True)
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


# ---- イベントハンドラ ----

@bot.event
async def on_ready():
    print(f"ログイン完了: {bot.user}  (ID: {bot.user.id})")
    print(f"参加サーバー: {len(bot.guilds)}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    key = (message.guild.id, message.author.id)
    now = datetime.utcnow()

    last = bot.xp_cooldowns.get(key)
    if last and (now - last).total_seconds() < XP_COOLDOWN_SECONDS:
        await bot.process_commands(message)
        return

    bot.xp_cooldowns[key] = now
    gain = random.randint(XP_PER_MESSAGE_MIN, XP_PER_MESSAGE_MAX)
    old_lv, new_lv, new_xp = await add_xp(message.guild.id, message.author.id, gain)

    if new_lv > old_lv:
        await send_levelup(message.guild, message.author, new_lv, new_xp, fallback=message.channel)

    await bot.process_commands(message)


@bot.event
async def on_thread_create(thread: discord.Thread):
    if not thread.guild or not thread.owner_id:
        return
    owner = thread.owner
    if owner is None or owner.bot:
        return

    old_lv, new_lv, new_xp = await add_xp(thread.guild.id, thread.owner_id, XP_PER_THREAD)

    if new_lv > old_lv:
        await send_levelup(thread.guild, owner, new_lv, new_xp, fallback=thread.parent or thread)


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.Member | discord.User):
    if user.bot or not reaction.message.guild:
        return

    old_lv, new_lv, new_xp = await add_xp(
        reaction.message.guild.id, user.id, XP_PER_REACTION
    )

    if new_lv > old_lv:
        await send_levelup(reaction.message.guild, user, new_lv, new_xp, fallback=reaction.message.channel)


# ---- スラッシュコマンド ----

@bot.tree.command(name="level", description="自分または指定ユーザーのレベルを確認")
@app_commands.describe(user="確認したいユーザー（省略すると自分）")
async def cmd_level(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    xp = await get_xp(interaction.guild_id, target.id)
    lv = get_level(xp)

    cur_total = total_xp_for_level(lv)
    next_total = total_xp_for_level(lv + 1)
    xp_in_lv = xp - cur_total
    xp_needed = next_total - cur_total
    progress = xp_in_lv / xp_needed if xp_needed else 1.0

    filled = int(progress * 20)
    bar = "█" * filled + "░" * (20 - filled)

    embed = discord.Embed(
        title=f"{target.display_name} のレベル情報",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="レベル", value=f"**{lv}**", inline=True)
    embed.add_field(name="合計 XP", value=f"{xp:,}", inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(
        name=f"進捗 ({xp_in_lv:,} / {xp_needed:,} XP)",
        value=f"`{bar}` {progress * 100:.1f}%",
        inline=False,
    )
    embed.add_field(name="次のレベルまで", value=f"{next_total - xp:,} XP", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="サーバーの XP ランキングを表示")
@app_commands.describe(page="ページ番号（デフォルト: 1）")
async def cmd_leaderboard(interaction: discord.Interaction, page: int = 1):
    per_page = 10
    offset = (page - 1) * per_page

    async with bot.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, xp FROM user_xp WHERE guild_id=$1 ORDER BY xp DESC LIMIT $2 OFFSET $3",
            interaction.guild_id, per_page, offset,
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM user_xp WHERE guild_id=$1",
            interaction.guild_id,
        )

    if not rows:
        await interaction.response.send_message("まだランキングデータがありません。", ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    total_pages = max(1, math.ceil(total / per_page))
    lines = []
    for i, row in enumerate(rows):
        rank = offset + i + 1
        medal = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
        lv = get_level(row["xp"])
        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"<@{row['user_id']}>"
        lines.append(f"{medal} {name} — Lv.**{lv}** ({row['xp']:,} XP)")

    embed = discord.Embed(
        title="🏆 XP ランキング",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"ページ {page} / {total_pages}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rank", description="サーバー内での自分の順位を確認")
async def cmd_rank(interaction: discord.Interaction):
    uid = interaction.user.id
    gid = interaction.guild_id
    xp = await get_xp(gid, uid)

    async with bot.db_pool.acquire() as conn:
        rank = await conn.fetchval(
            "SELECT COUNT(*) FROM user_xp WHERE guild_id=$1 AND xp > $2",
            gid, xp,
        )
    rank += 1

    lv = get_level(xp)
    embed = discord.Embed(
        title=f"{interaction.user.display_name} の順位",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="順位", value=f"**{rank} 位**", inline=True)
    embed.add_field(name="レベル", value=f"**{lv}**", inline=True)
    embed.add_field(name="XP", value=f"{xp:,}", inline=True)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        print("エラー: DISCORD_TOKEN が設定されていません。")
    elif not DATABASE_URL:
        print("エラー: DATABASE_URL が設定されていません。")
    else:
        bot.run(TOKEN)
