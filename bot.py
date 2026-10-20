import aiohttp
import discord
from discord.ext import commands, tasks
import asyncio
import contextlib
import datetime
import functools
import io
import logging
import random
import re
import time
from zoneinfo import ZoneInfo
import responses
import botFunctions as bf
import channel_config
import economy
import game_news
import link_preview
import llmask
import memory_db
import sports
import sportsbook
import steam_sales
import update_log
import f1
import jra
import jra_bets
import nfl_props
import os
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# Exact cyan, used as every embed's left-hand accent color for a consistent look.
EMBED_COLOR = discord.Color.from_rgb(0, 255, 255)

# Matches a direct link to an image file (as opposed to a page that merely
# contains one) -- used to display /chat's image_search results inline in
# the embed itself rather than just as a clickable text link.
IMAGE_URL_RE = re.compile(r'https?://\S+\.(?:jpg|jpeg|png|gif|webp)\b', re.IGNORECASE)

# Sites Discord natively unfurls into a rich preview (video thumbnail +
# player for YouTube, full card for a tweet/X post) -- but only when the
# URL is in a plain message's raw content, never when it's just text inside
# an embed's description (same reason /vini below is sent as plain text
# instead of an embed). /chat's citations are a dynamic URL the model
# picks, not a fixed one, so instead of leaving it as dead text inside the
# embed, it's also re-sent as its own plain message so Discord unfurls it.
RICH_PREVIEW_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/watch\S*|youtu\.be/\S+|(?:twitter|x)\.com/\w+/status/\d+\S*)',
    re.IGNORECASE,
)

# The link in an announcement whose artwork _send_announcement looks up
# (via link_preview) and shows inside the embed. Unscoped to specific sites,
# unlike RICH_PREVIEW_URL_RE: announcements link to Spotify, YouTube, news
# articles, Steam and more, and link_preview handles each on its own terms.
ANNOUNCEMENT_URL_RE = re.compile(r'https?://\S+')

MAX_EMBED_DESC = 4096  # Discord's hard cap on an embed description

# Opt-in marker any announcement string can end with to give its embed
# artwork: a line reading "IMAGE: <url>". _embed strips it back out before
# the text is shown, so a module that has cover art or a store banner to
# hand can attach it without every _broadcast caller having to learn a
# richer message type than "a string".
#
# Deliberately set_image and not set_thumbnail: the thumbnail slot is a
# small square in the embed's corner, which rendered a 460x215 store
# banner at postage-stamp size. set_image gives it the embed's full width.
IMAGE_MARKER_RE = re.compile(r'^IMAGE:\s*(\S+)\s*$', re.MULTILINE)

# Pixel width /recsong and SOTD album art is shrunk to (see
# _send_announcement). Raise it for a bigger cover, lower it for smaller;
# anything past ~400 just fills the embed's full width.
ALBUM_ART_SIZE = 200


def _with_question(question_line: str, body: str) -> str:
    """Joins /chat's echoed question and its status/answer into a single
    embed description, so the two show as one box instead of two separate
    messages. Truncates the question (never the body) if the combination
    would exceed Discord's embed description cap -- the body is what
    actually matters; the question is just context above it."""
    available = MAX_EMBED_DESC - len(body) - 2  # 2 for the blank line between them
    if available <= 0:
        return body
    if len(question_line) > available:
        question_line = question_line[:available - 1].rstrip() + "…"
    return f"{question_line}\n\n{body}"


def _format_elapsed(seconds: float) -> str:
    """Formats a /chat response time for the embed footer -- e.g. "2.4s"
    or "1.3m" for anything a minute or longer, since gemma3 can
    legitimately take a while on a multi-step tool lookup."""
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds / 60:.1f}m"


def _embed(text: str) -> discord.Embed:
    """Wraps plain text in a cyan-bordered embed -- the standard shape for
    every bot response. Existing sports.py/llmask.py output is already
    sized for Discord's 2000-char plain-message limit, well under an
    embed description's 4096-char cap, so no re-chunking is needed.

    A trailing "IMAGE: <url>" line becomes the embed's full-width image
    and is stripped from the visible text. Failing that, a direct image
    URL in the text itself (e.g. /chat citing an image_search result) is
    shown the same way rather than left as a plain link -- an explicit
    marker wins, so a module that named its artwork doesn't have it
    replaced by some other .jpg that happens to appear in the body."""
    artwork_match = IMAGE_MARKER_RE.search(text)
    artwork_url = None
    if artwork_match:
        artwork_url = artwork_match.group(1)
        text = IMAGE_MARKER_RE.sub('', text).strip()

    e = discord.Embed(description=text, color=EMBED_COLOR)
    if artwork_url:
        e.set_image(url=artwork_url)
    else:
        image_match = IMAGE_URL_RE.search(text)
        if image_match:
            e.set_image(url=image_match.group(0))
    return e


async def _send_announcement(destination, message: str, artwork_as_thumbnail: bool = False):
    """Sends an announcement as a single embed carrying its link's own
    artwork -- album art for a Spotify/YouTube track, an article's hero
    image -- so the link appears once, inside the box.

    Always exactly one message. There used to be a fallback that re-posted
    the bare link underneath when no artwork could be found, so Discord
    would draw its own preview card instead -- but that's the duplicate
    link this exists to get rid of, and it fired precisely when the lookup
    failed, which is when it was least wanted. With no artwork the embed
    still carries its link, clickable, just without a picture;
    link_preview logs why, so a run of missing pictures is diagnosable
    from the bot's log. destination can be a channel or a command Context
    -- both expose a compatible .send()."""
    embed = _embed(message)
    file = None

    # A module that already named its own artwork (Steam, comic covers)
    # has nothing to look up.
    if not (embed.thumbnail.url or embed.image.url):
        url_match = ANNOUNCEMENT_URL_RE.search(message)
        if url_match:
            artwork = await asyncio.to_thread(link_preview.artwork_for, url_match.group(0))
            if artwork and artwork_as_thumbnail:
                # A square album cover at full embed width dwarfs the text,
                # and the corner thumbnail is too small to make out. An
                # uploaded pre-shrunk copy is drawn at its own size, in
                # between; the thumbnail is the fallback if that fails.
                shrunk = await asyncio.to_thread(link_preview.shrink_artwork, artwork, ALBUM_ART_SIZE)
                if shrunk:
                    file = discord.File(io.BytesIO(shrunk), filename="artwork.png")
                    embed.set_image(url="attachment://artwork.png")
                else:
                    embed.set_thumbnail(url=artwork)
            elif artwork:
                embed.set_image(url=artwork)

    if file:
        await destination.send(embed=embed, file=file)
    else:
        await destination.send(embed=embed)


def _split_by_line(text: str, limit: int = MAX_EMBED_DESC) -> list[str]:
    """Splits text into chunks of whole lines that each fit an embed
    description -- for a list that can outgrow one embed, like a heavy
    bettor's /mybets, which Discord would otherwise reject outright."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            candidate = line
        current = candidate
    chunks.append(current)
    return chunks


# What tasks.loop already retries by itself, after a back-off.
_LOOP_RETRIED = (OSError, discord.GatewayNotFound, discord.ConnectionClosed, aiohttp.ClientError, asyncio.TimeoutError)


def _keep_alive(body):
    """For every tasks.loop body. Anything else a loop raises stops it for
    good, so one bad poll -- a reshaped API response, a corrupt state file
    -- would switch that feature off until the next restart. Logged
    instead, and the loop runs again on schedule."""
    @functools.wraps(body)
    async def wrapper(*args, **kwargs):
        try:
            await body(*args, **kwargs)
        except _LOOP_RETRIED:
            raise
        except Exception:
            logger.exception(f"{body.__name__}: this run failed; it'll run again on schedule")
    return wrapper

# Commands that work normally but are left out of /help entirely -- easter
# eggs that only show up if you already know about them.
HIDDEN_COMMANDS = {'vini'}

# The music commands and Song of the Day belong to one server and no other.
# Their slash commands are registered to that server alone, so anywhere
# else they don't exist at all; the ! versions do nothing elsewhere; and
# Song of the Day can't be set up or sent anywhere else. /help only lists
# them in that server.
MUSIC_GUILD_ID = 748160430828027916
MUSIC_COMMANDS = {'recsong', 'top5songs'}
MUSIC_FEATURES = {'sotd'}


def _features_for(guild_id) -> dict[str, str]:
    """The announcement features this server can set up: every one, minus
    the music ones anywhere but the music server."""
    return {
        key: label for key, label in channel_config.FEATURES.items()
        if key not in MUSIC_FEATURES or guild_id == MUSIC_GUILD_ID
    }

# Private maintainer channel for the deploy changelog (see update_log.py).
# Hardcoded on purpose: this is deliberately NOT a /setchannel feature and
# has no command of its own, so no other server can opt into it and it
# never appears in /help. A channel id resolves to exactly one channel
# across all of Discord, so there's no way for this to fan out.
UPDATE_LOG_CHANNEL_ID = 1552777614111014972

# Which section of /help each command is listed under. Anything not listed
# here falls into an "Other" section so a forgotten new command still shows
# up instead of silently vanishing from the list.
COMMAND_CATEGORIES = {
    'nfl': 'Sports',
    'nfllive': 'Sports',
    'nflresults': 'Sports',
    'nflstandings': 'Sports',
    'cfb': 'Sports',
    'mlb': 'Sports',
    'soccer': 'Sports',
    'livesoccer': 'Sports',
    'soccerresults': 'Sports',
    'prem': 'Sports',
    'laliga': 'Sports',
    'ucl': 'Sports',
    'f1': 'Sports',
    'f1standings': 'Sports',
    'jra': 'Sports',
    'jraodds': 'Sports',
    'recsong': 'Music',
    'top5songs': 'Music',
    'hello': 'Fun',
    'rolld6': 'Fun',
    'rolld20': 'Fun',
    'ping': 'Fun',
    'coin_flip': 'Fun',
    '8ball': 'Fun',
    'work': 'Economy',
    'mine': 'Economy',
    'balance': 'Economy',
    'donate': 'Economy',
    'slots': 'Economy',
    'roulette': 'Economy',
    'blackjack': 'Economy',
    'poker': 'Economy',
    'bet': 'Economy',
    'horsebet': 'Economy',
    'props': 'Economy',
    'propbet': 'Economy',
    'mybets': 'Economy',
    'leaderboard': 'Economy',
    'mcstatus': 'Other',
    'steamsales': 'Other',
    'setchannel': 'Other',
    'chat': 'AI',
    'forget': 'AI',
    'forgetme': 'AI',
}
CATEGORY_ORDER = ['Sports', 'Music', 'Fun', 'Economy', 'AI', 'Other']

# Folder that contains this script, so file paths work on Windows and Linux
BASE = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE, 'code.env')
load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise RuntimeError(f"DISCORD_TOKEN not found. Make sure it is set in {os.path.abspath(ENV_PATH)}")


class PaginatorView(discord.ui.View):
    """Shows a list of (title, page_text) pages one at a time, flipped
    through with buttons -- used by any command that'd otherwise need a
    separate command per page (e.g. /soccer per competition, /nflstandings
    per view)."""

    def __init__(self, pages, author_id, command_name):
        super().__init__(timeout=180)
        self.pages = pages  # list of (title, page_text)
        self.index = 0
        self.author_id = author_id
        self.command_name = command_name
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index == len(self.pages) - 1

    def embed(self) -> discord.Embed:
        title, body = self.pages[self.index]
        e = _embed(body)
        e.set_footer(text=f"Page {self.index + 1}/{len(self.pages)} — {title}")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"Only the person who ran /{self.command_name} can flip pages.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# What the bot needs in a channel to post announcements there: to see it,
# send in it, and send embeds -- plus attach files, since Song of the Day's
# album art goes up as an upload.
ANNOUNCE_PERMISSIONS = {
    'view_channel': "View Channel",
    'send_messages': "Send Messages",
    'embed_links': "Embed Links",
    'attach_files': "Attach Files",
}


def _missing_announce_permissions(channel, me: discord.Member) -> list[str]:
    permissions = channel.permissions_for(me)
    return [label for attr, label in ANNOUNCE_PERMISSIONS.items() if not getattr(permissions, attr)]


def _not_a_member_notice(client: discord.Client, guild_id: int) -> str:
    """For a server where only the bot's slash commands are installed, with
    no bot member: an app added for its commands alone, or a bot that was
    removed. Commands still reply there -- Discord delivers them either way
    -- but nothing can post in it on its own, announcements included. The
    link adds the bot as a member (the "bot" scope a commands-only install
    leaves out), preset to this server, with what announcements need."""
    invite = discord.utils.oauth_url(
        client.user.id,
        permissions=discord.Permissions(**dict.fromkeys(ANNOUNCE_PERMISSIONS, True)),
        guild=discord.Object(id=guild_id),
        scopes=('bot', 'applications.commands'),
        disable_guild_select=True,
    )
    return (
        "⚠️ **I'm not actually in this server** — only my slash commands are, which is why they "
        "reply but nothing gets posted here on its own. "
        f"**[Add me to the server]({invite})** and announcements will start going to the channels below."
    )


def _announcement_status(guild_id: int, client: discord.Client) -> str:
    """Where each announcement feature posts in this server, and anything
    stopping it. Every feature is set up separately per server, so one that
    was never set up here looks exactly like one that's broken -- this is
    what tells them apart."""
    configured = channel_config.get_for_guild(guild_id)
    # Looked up by id rather than taken from the command: a server the bot
    # is in always comes from its own cache, while one it isn't in reaches a
    # command as an empty stand-in with no channels -- so every channel
    # would read as deleted when the real problem is the missing bot.
    member_of = client.get_guild(guild_id)
    lines = [_not_a_member_notice(client, guild_id), ""] if member_of is None else []
    lines.append("**Announcements in this server**")
    for key, label in _features_for(guild_id).items():
        if key not in configured:
            lines.append(f"❌ {label} — not set up")
            continue
        if member_of is None:
            lines.append(f"⏸️ {label} → <#{configured[key]}>, once I'm in")
            continue
        channel = member_of.get_channel(configured[key])
        if channel is None:
            lines.append(f"⚠️ {label} — its channel was deleted")
            continue
        missing = _missing_announce_permissions(channel, member_of.me)
        if missing:
            lines.append(f"⚠️ {label} → {channel.mention}, but I'm missing {', '.join(missing)} there")
        else:
            lines.append(f"✅ {label} → {channel.mention}")
    return "\n".join(lines)


# (guild id, Eastern date) pairs already caught up on today's F1 race day,
# so a bot re-added twice in a day, or /setchannel run again, can't post
# it twice. In memory only: a restart forgetting it costs one repeat.
_f1_caught_up: set[tuple[int, datetime.date]] = set()


async def _f1_race_day_catch_up(client: discord.Client, guild_id: int) -> None:
    """Posts today's F1 race-day announcement in a server that's only just
    started getting F1 updates -- the bot newly added, or the feature newly
    set up -- since it went out to every other server before this one was
    listening. Nothing on any other day. Never raises."""
    key = (guild_id, datetime.datetime.now(EASTERN).date())
    channel_id = channel_config.get_channel(guild_id, 'f1_updates')
    if channel_id is None or key in _f1_caught_up:
        return
    messages = await asyncio.to_thread(f1.race_day_catch_up)
    if not messages:
        return
    _f1_caught_up.add(key)  # before the first await, so two triggers at once can't both post
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        for message in messages:
            await channel.send(embed=_embed(message))
    except Exception as e:
        _f1_caught_up.discard(key)
        logger.warning(f"f1: couldn't post today's race day in guild {guild_id}: {e}")


class _SetChannelFeatureSelect(discord.ui.Select):
    """Step 1 of /setchannel: pick which features to set up -- one, or
    several to send them all to the same channel at once."""

    def __init__(self, parent_view: "SetChannelView"):
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in _features_for(parent_view.guild_id).items()
        ]
        super().__init__(
            placeholder="Step 1: choose features to set up...",
            options=options, min_values=1, max_values=len(options),
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.features = list(self.values)
        labels = ", ".join(f"**{channel_config.FEATURES[key]}**" for key in self.values)
        self.parent_view.clear_items()
        self.parent_view.add_item(_SetChannelChannelSelect(self.parent_view))
        await interaction.response.edit_message(
            embed=_embed(f"{labels} selected. Now pick a channel:"),
            view=self.parent_view,
        )


class _SetChannelChannelSelect(discord.ui.ChannelSelect):
    """Step 2 of /setchannel: pick the destination channel via Discord's
    own native channel picker -- no typing or ID-copying required."""

    def __init__(self, parent_view: "SetChannelView"):
        super().__init__(
            placeholder="Step 2: choose a channel...",
            # Announcement channels too: it's where a lot of servers would
            # want these, and the bot posts in one the same way.
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # Acknowledged first: the checks and the test post below are more
        # requests, and an interaction has to be answered within 3 seconds.
        await interaction.response.defer()
        picked = self.values[0]
        labels = ", ".join(f"**{channel_config.FEATURES[key]}**" for key in self.parent_view.features)

        # guild_id comes straight off the interaction payload; the guild
        # itself only exists in the cache if the bot is really a member.
        guild = interaction.client.get_guild(interaction.guild_id)
        if guild is None:
            # Not saved as if it worked: nothing can post here until the bot
            # is added. Anything already set up still shows, waiting.
            self.parent_view.clear_items()
            await interaction.edit_original_response(
                embed=_embed(_announcement_status(interaction.guild_id, interaction.client)),
                view=self.parent_view,
            )
            self.parent_view.stop()
            return

        channel = guild.get_channel(picked.id)
        problem = None
        if channel is None:
            problem = f"I can't see {picked.mention}."
        elif missing := _missing_announce_permissions(channel, guild.me):
            problem = f"I can't post in {picked.mention} — I'm missing **{', '.join(missing)}** there."
        if problem:
            # Not saved: announcements sent there would only fail quietly.
            # The picker stays up to choose another channel instead.
            await interaction.edit_original_response(
                embed=_embed(f"⚠️ {problem} Give me access there, or pick another channel for {labels}:"),
                view=self.parent_view,
            )
            return

        f1_moved_here = (
            'f1_updates' in self.parent_view.features
            and channel_config.get_channel(guild.id, 'f1_updates') != channel.id
        )
        for key in self.parent_view.features:
            channel_config.set_channel(guild.id, key, channel.id)

        # A real post, not just a permission check, so it's plain right away
        # whether announcements will actually show up there.
        try:
            await channel.send(embed=_embed(f"📣 Announcements set up here: {labels}."))
            result = f"✅ {labels} will now post in {channel.mention}. I posted a test message there."
        except discord.HTTPException as e:
            result = (
                f"⚠️ Saved, but my test message in {channel.mention} failed: {e.text or e}. "
                f"Announcements won't show up there until that's fixed."
            )

        self.parent_view.clear_items()
        await interaction.edit_original_response(
            embed=_embed(f"{result}\n\n{_announcement_status(guild.id, interaction.client)}"),
            view=self.parent_view,
        )
        self.parent_view.stop()
        if f1_moved_here:
            # After the reply, since it fetches results: if today's a race
            # day, this channel shouldn't have to wait for next weekend.
            await _f1_race_day_catch_up(interaction.client, guild.id)


class SetChannelView(discord.ui.View):
    """Backs /setchannel's guided flow: pick features from a dropdown,
    then a channel from a native Discord picker, one step at a time --
    instead of typing feature and channel as command arguments."""

    def __init__(self, author_id, guild_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.guild_id = guild_id
        self.features: list[str] = []
        self.message: discord.Message | None = None
        self.add_item(_SetChannelFeatureSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran /setchannel can use this.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def _in_music_guild(ctx: commands.Context) -> bool:
    return ctx.guild is not None and ctx.guild.id == MUSIC_GUILD_ID


async def _take_bet(ctx: commands.Context, bet: int) -> bool:
    """Takes a bet out of the player's wallet before a game is played,
    or tells them they can't afford it and returns False."""
    if economy.take_bet(ctx.guild.id, ctx.author.id, bet):
        return True
    coins = economy.get_balance(ctx.guild.id, ctx.author.id)
    await ctx.send(
        embed=_embed(
            f"You only have {economy.format_coins(coins)} — not enough to bet "
            f"{economy.format_coins(bet)}. /work pays **{economy.WORK_MIN}–{economy.WORK_MAX}** "
            f"{economy.COIN} every {economy.WORK_EVERY}, and /mine pays "
            f"**{economy.MINE_MIN}–{economy.MINE_MAX}** {economy.COIN} up to {economy.MINE_LIMIT} times "
            f"every {economy.MINE_WINDOW_MINUTES} minutes."
        ),
        ephemeral=True,
    )
    return False


class BlackjackView(discord.ui.View):
    """Hit / Stand / Double Down buttons for one /blackjack hand.

    The bet is already out of the player's wallet by the time this
    exists, and settle() pays back whatever the hand returns exactly
    once, when it ends. Walking away doesn't forfeit: on timeout the
    player stands on what they're holding. A bot restart mid-hand does
    lose the bet, since the hand only lives in memory."""

    def __init__(self, game: economy.BlackjackGame, guild_id: int, author: discord.abc.User):
        super().__init__(timeout=120)
        self.game = game
        self.guild_id = guild_id
        self.author = author
        self.original_bet = game.bet
        self.balance: int | None = None  # set by settle()
        self.timed_out = False
        self.message: discord.Message | None = None
        self._refresh_buttons()

    def settle(self):
        if self.game.finished and self.balance is None:
            self.balance = economy.pay(self.guild_id, self.author.id, self.game.payout())

    def embed(self) -> discord.Embed:
        lines = [f"🃏 **Blackjack** — {self.author.display_name} bets {economy.format_coins(self.game.bet)}", "", self.game.render()]
        if self.game.finished:
            lines.append("")
            if self.timed_out:
                lines.append("⏱️ Took too long, so you stood automatically.")
            lines.append(self.game.result_line())
            lines.append(economy.outcome_line(self.game.bet, self.game.payout(), self.balance))
        return _embed("\n".join(lines))

    def _refresh_buttons(self):
        if self.game.finished:
            self.clear_items()
        else:
            self.double_button.disabled = not self.game.can_double

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the person who ran /blackjack can play this hand.", ephemeral=True
            )
            return False
        # Two quick clicks can both be queued before the first one ends
        # the hand -- the second must not deal into a hand that's paid out.
        if self.game.finished:
            await interaction.response.defer()
            return False
        return True

    async def _show(self, interaction: discord.Interaction):
        self.settle()
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if self.game.finished:
            self.stop()

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.game.hit()
        await self._show(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.game.stand()
        await self._show(interaction)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.success)
    async def double_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Hit and Double Down clicked together are both queued, so by now a
        # third card may be out -- and doubling is only on the first two.
        if not self.game.can_double:
            await interaction.response.defer()
            return
        if not economy.take_bet(self.guild_id, self.author.id, self.original_bet):
            await interaction.response.send_message(
                f"Doubling down costs another {economy.format_coins(self.original_bet)}, and you don't have it.",
                ephemeral=True,
            )
            return
        self.game.double_down()
        await self._show(interaction)

    async def on_timeout(self):
        if not self.game.finished:
            self.timed_out = True
            self.game.stand()
        self.settle()
        self._refresh_buttons()
        if self.message:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass


class PokerView(discord.ui.View):
    """A hold button per card plus Draw, for one /poker hand.

    Same money handling as BlackjackView: the bet is already taken, and
    settle() pays out exactly once, after the draw. Walking away doesn't
    forfeit: on timeout it draws with whatever was being held."""

    def __init__(self, game: economy.PokerGame, guild_id: int, author: discord.abc.User):
        super().__init__(timeout=120)
        self.game = game
        self.guild_id = guild_id
        self.author = author
        self.balance: int | None = None  # set by settle()
        self.timed_out = False
        self.message: discord.Message | None = None
        self.card_buttons = []
        for index in range(5):
            button = discord.ui.Button(row=0)
            button.callback = self._hold_callback(index)
            self.add_item(button)
            self.card_buttons.append(button)
        self._refresh_buttons()

    def _hold_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            self.game.toggle_hold(index)
            await self._show(interaction)
        return callback

    def settle(self):
        if self.game.finished and self.balance is None:
            self.balance = economy.pay(self.guild_id, self.author.id, self.game.payout())

    def embed(self) -> discord.Embed:
        game = self.game
        lines = [
            f"♠️ **Video Poker** — {self.author.display_name} bets {economy.format_coins(game.bet)}",
            "",
            f"# {' '.join(game.cards)}",
        ]
        if not game.finished:
            lines.append(f"Dealt: **{game.hand}**" if game.hand else "Dealt: no paying hand yet")
            lines.append("Tap the cards to hold, then **Draw** to replace the rest.")
        else:
            if self.timed_out:
                lines.append("⏱️ Took too long, so it drew with the cards you were holding.")
            lines.append(game.result_line())
            lines.append(economy.outcome_line(game.bet, game.payout(), self.balance))
        return _embed("\n".join(lines))

    def _refresh_buttons(self):
        if self.game.finished:
            self.clear_items()
            return
        for button, card, held in zip(self.card_buttons, self.game.cards, self.game.held):
            button.label = f"{card} ✓" if held else card
            button.style = discord.ButtonStyle.success if held else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the person who ran /poker can play this hand.", ephemeral=True
            )
            return False
        # A hold clicked just as Draw lands must not touch a paid-out hand.
        if self.game.finished:
            await interaction.response.defer()
            return False
        return True

    async def _show(self, interaction: discord.Interaction):
        self.settle()
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if self.game.finished:
            self.stop()

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.primary, row=1)
    async def draw_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.game.draw()
        await self._show(interaction)

    async def on_timeout(self):
        if not self.game.finished:
            self.timed_out = True
            self.game.draw()
        self.settle()
        self._refresh_buttons()
        if self.message:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass


def run_discord_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    client = commands.Bot(command_prefix=['!', '/'], intents=intents, help_command=None)

 #=========================--COMMANDS (slash + ! text)--=====================#
 # hybrid_command registers each one as both a /slash command and a
 # !text command from a single function, instead of maintaining two copies.

    @client.hybrid_command(name="hello", description="Says hello")
    async def hello(ctx: commands.Context):
        await ctx.send(embed=_embed("Hello!"))

    @client.hybrid_command(name="recsong", description="Recommends a random song from the server's list")
    @discord.app_commands.guilds(MUSIC_GUILD_ID)
    @commands.check(_in_music_guild)
    async def recsong(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.recsongs, random.choice(bf.RECSONG_HEADINGS))
        await _send_announcement(ctx, result, artwork_as_thumbnail=True)

    @client.hybrid_command(name="top5songs", description="Top 5 songs on iTunes charts!")
    @discord.app_commands.guilds(MUSIC_GUILD_ID)
    @commands.check(_in_music_guild)
    async def top5(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.topsongs)
        await _send_announcement(ctx, result)

    async def music_error(ctx: commands.Context, error: commands.CommandError):
        # !recsong anywhere but the music server: act as if it doesn't exist.
        if not isinstance(error, commands.CheckFailure):
            raise error

    for command in (recsong, top5):
        command.error(music_error)

    @client.hybrid_command(name='rolld6', description='Rolls a D6 dice')
    async def rolld6(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.rolld6()))

    @client.hybrid_command(name='rolld20', description='Rolls a D20 dice')
    async def rolld20(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.rolld20()))

    @client.hybrid_command(name='ping', description='pingpong')
    async def ping(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.ping()))

    @client.hybrid_command(name="coin_flip", description="Flip a coin!")
    async def coinflip(ctx: commands.Context):
        await ctx.send(embed=_embed(responses.coinflip()))

    @client.hybrid_command(name="vini", description="Posts a Vini Jr tweet")
    async def vini(ctx: commands.Context):
        # Deliberately plain text, not an embed -- Discord only auto-unfurls
        # a link into its own rich preview when it's raw message content,
        # not when it's tucked inside an embed description.
        await ctx.send("https://x.com/vinijr/status/1851023004496789695?s=20")

    @client.hybrid_command(name="8ball", description="Shakes an eight ball")
    @discord.app_commands.describe(question="What do you want to ask the eight ball?")
    async def eightball(ctx: commands.Context, *, question: str = None):
        if question:
            await ctx.send(embed=_embed(f"**From {ctx.author.display_name}:** {question}"))
        await ctx.send(embed=_embed(bf.eightball()))

    @client.hybrid_command(name="nfl", description="This week's NFL games and live scores")
    async def nfl(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="nfllive", description="Only NFL games currently in progress")
    async def nfllive(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_live_matches)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="nflresults", description="This week's finished NFL games and final scores")
    async def nflresults(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.nfl_results_this_week)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="cfb", description="This week's ranked college football games, with a South Florida spotlight")
    async def cfb(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.cfb_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="mlb", description="This week's MLB series and their records")
    async def mlb(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.mlb_series_synopsis)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="soccer", description="This week's matches, one page per competition")
    async def soccer(ctx: commands.Context):
        await ctx.defer()
        pages = await asyncio.to_thread(sports.soccer_pages)
        if not pages:
            await ctx.send(embed=_embed("No matches scheduled in any tracked competition this week."))
            return
        view = PaginatorView(pages, author_id=ctx.author.id, command_name="soccer")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

    @client.hybrid_command(name="steamsales", description="Popular games currently on sale on Steam")
    async def steamsales(ctx: commands.Context):
        await ctx.defer()
        pages = await asyncio.to_thread(steam_sales.current_sales_pages)
        if not pages:
            await ctx.send(embed=_embed(
                "No popular Steam sales going on right now — or Steam isn't answering. Try again later!"
            ))
            return
        view = PaginatorView(pages, author_id=ctx.author.id, command_name="steamsales")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

    @client.hybrid_command(name="nflstandings", description="NFL standings by division, and the current playoff picture")
    async def nflstandings(ctx: commands.Context):
        await ctx.defer()
        pages = await asyncio.to_thread(sports.nfl_standings_pages)
        view = PaginatorView(pages, author_id=ctx.author.id, command_name="nflstandings")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

    @client.hybrid_command(name="livesoccer", description="Only soccer matches currently in progress")
    async def livesoccer(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.live_soccer_matches)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="soccerresults", description="Today's finished soccer matches and final scores")
    async def soccerresults(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.soccer_results_today)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="prem", description="Current Premier League standings")
    async def prem(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.premier_league_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="laliga", description="Current La Liga standings")
    async def laliga(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.la_liga_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="ucl", description="Current Champions League standings or bracket")
    async def ucl(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(sports.ucl_table)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="f1", description="Is it F1 race weekend right now?")
    async def f1cmd(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(f1.f1_status)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="f1standings", description="Current F1 drivers' and constructors' championship standings")
    async def f1standings(ctx: commands.Context):
        await ctx.defer()
        messages = await asyncio.to_thread(f1.f1_standings)
        for message in messages:
            await ctx.send(embed=_embed(message))

    @client.hybrid_command(name="jra", description="Upcoming Japanese (JRA) graded stakes horse races")
    async def jracmd(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(jra.jra_schedule)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="jraodds", description="A JRA race's field and each horse's chance of winning")
    @discord.app_commands.describe(race="Which race — leave empty for the next one up")
    async def jraodds(ctx: commands.Context, *, race: str | None = None):
        await ctx.defer()
        result = await asyncio.to_thread(jra.jra_race, race)
        await ctx.send(embed=_embed(result))

    @jraodds.autocomplete('race')
    async def jraodds_race_autocomplete(interaction: discord.Interaction, current: str):
        choices = await asyncio.to_thread(jra.race_choices, current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in choices]

    @client.hybrid_command(name="mcstatus", description="Check the Minecraft server status")
    async def mcstatus(ctx: commands.Context):
        await ctx.defer()
        result = await asyncio.to_thread(bf.mc_status)
        await ctx.send(embed=_embed(result))

    @client.hybrid_command(name="setchannel", description="See where announcements post in this server, and set them up")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def setchannel(ctx: commands.Context):
        view = SetChannelView(author_id=ctx.author.id, guild_id=ctx.guild.id)
        message = await ctx.send(
            embed=_embed(
                f"{_announcement_status(ctx.guild.id, ctx.bot)}\n\n"
                "Step 1: choose the features to set up — pick several to send them all to one channel."
            ),
            view=view,
        )
        view.message = message

    @setchannel.error
    async def setchannel_error(ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=_embed("You need the Manage Server permission to do that."), ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=_embed("This can only be used in a server, not a DM."), ephemeral=True)
        else:
            raise error

    @client.hybrid_command(name="chat", description="Chat with DJ Shinx's AI brain")
    @discord.app_commands.describe(message="What do you want to say?")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def chat(ctx: commands.Context, *, message: str):
        question_line = f"**From {ctx.author.display_name}:** {message}"
        thinking_message = await ctx.send(embed=_embed(_with_question(question_line, "🧠 Thinking")))
        conversation_id = (ctx.channel.id, ctx.author.id)

        # llmask.ask() runs in a worker thread and calls on_status from
        # there -- it just records the latest label, rather than editing
        # Discord directly, since the actual edits (and the animated dots)
        # are driven by _animate_thinking below on the main event loop.
        status = {'label': "🧠 Thinking"}

        def on_status(text: str):
            status['label'] = text.rstrip('.')

        async def animate_thinking():
            dots = 0
            while True:
                await asyncio.sleep(0.5)
                dots = dots % 3 + 1
                try:
                    await thinking_message.edit(
                        embed=_embed(_with_question(question_line, status['label'] + '.' * dots))
                    )
                except discord.HTTPException:
                    pass

        animation_task = asyncio.create_task(animate_thinking())
        start_time = time.monotonic()
        try:
            result, chart_path = await asyncio.to_thread(llmask.ask, message, conversation_id, on_status, ctx.author.id)
        finally:
            elapsed = time.monotonic() - start_time
            animation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await animation_task

        chunks = llmask.chunk_response(result)

        first_embed = _embed(_with_question(question_line, chunks[0]))
        first_embed.set_footer(text=f"Answered in {_format_elapsed(elapsed)}")
        if chart_path:
            # A locally-rendered chart has no public URL to point an embed
            # at -- attach the file itself and reference it via Discord's
            # attachment:// scheme instead, then drop the local temp file
            # now that Discord has its own copy.
            filename = os.path.basename(chart_path)
            first_embed.set_image(url=f"attachment://{filename}")
            await thinking_message.edit(embed=first_embed, attachments=[discord.File(chart_path, filename=filename)])
            try:
                os.remove(chart_path)
            except OSError:
                pass
        else:
            await thinking_message.edit(embed=first_embed)

        for chunk in chunks[1:]:
            await ctx.send(embed=_embed(chunk))

        # If the answer cited a YouTube/X link, also send it as its own
        # plain message so Discord's native preview actually renders --
        # see RICH_PREVIEW_URL_RE above for why that can't happen from
        # inside the embed itself.
        preview_match = RICH_PREVIEW_URL_RE.search(result)
        if preview_match:
            await ctx.send(preview_match.group(0))

    @chat.error
    async def chat_error(ctx: commands.Context, error: commands.CommandError):
        # Ollama requests are serialized behind one lock (see llmask.py),
        # so one person spamming /chat directly slows down everyone else's
        # queue -- this cooldown is what actually protects that, this just
        # reports it instead of leaving the interaction failing silently.
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=_embed(f"Slow down a bit — try again in {error.retry_after:.0f}s."),
                ephemeral=True,
            )
        else:
            raise error

    @client.hybrid_command(name="forget", description="Clears your conversation history with DJ Shinx's AI brain")
    async def forget(ctx: commands.Context):
        conversation_id = (ctx.channel.id, ctx.author.id)
        await asyncio.to_thread(llmask.forget, conversation_id)
        await ctx.send(embed=_embed("Alright, clean slate — I've forgotten our conversation so far."))

    @client.hybrid_command(name="forgetme", description="Deletes everything Agent Shinx has remembered about you long-term")
    async def forgetme(ctx: commands.Context):
        deleted = await asyncio.to_thread(memory_db.clear_facts, ctx.author.id)
        if deleted:
            await ctx.send(embed=_embed(f"Done — deleted {deleted} thing{'s' if deleted != 1 else ''} I'd remembered about you."))
        else:
            await ctx.send(embed=_embed("There wasn't anything long-term saved about you to delete."))

    # Economy: play money per server (see economy.py). Every command is
    # guild_only because balances belong to a server, and a DM has none.

    @client.hybrid_command(
        name="work",
        description=f"Earn {economy.WORK_MIN}–{economy.WORK_MAX} coins — every {economy.WORK_EVERY}",
    )
    @commands.guild_only()
    async def work(ctx: commands.Context):
        result = economy.work(ctx.guild.id, ctx.author.id)
        if result is None:
            wait = economy.format_duration(economy.time_until_next_work(ctx.guild.id, ctx.author.id))
            await ctx.send(embed=_embed(f"You're on break. Your next shift starts in **{wait}**."), ephemeral=True)
            return
        earned, coins = result
        job = random.choice(economy.WORK_JOBS)
        await ctx.send(embed=_embed(
            f"💼 You {job} and earned {economy.format_coins(earned)}.\n"
            f"Balance: {economy.format_coins(coins)}"
        ))

    @client.hybrid_command(
        name="mine",
        description=(
            f"Earn {economy.MINE_MIN}–{economy.MINE_MAX} coins — up to {economy.MINE_LIMIT} times "
            f"every {economy.MINE_WINDOW_MINUTES} minutes"
        ),
    )
    @commands.guild_only()
    # Per member per server, like wallets. Kept in memory, like /chat's, so
    # a bot restart hands everyone a fresh set of mines -- a few hundred
    # coins at most, not worth tracking in economy.db.
    @commands.cooldown(economy.MINE_LIMIT, economy.MINE_WINDOW_MINUTES * 60, commands.BucketType.member)
    async def mine(ctx: commands.Context):
        earned, coins = economy.mine(ctx.guild.id, ctx.author.id)
        find = random.choice(economy.MINE_FINDS)
        await ctx.send(embed=_embed(
            f"⛏️ You dug up {find} and sold it for {economy.format_coins(earned)}.\n"
            f"Balance: {economy.format_coins(coins)}"
        ))

    @client.hybrid_command(name="balance", description="How many coins you (or someone else) have")
    @discord.app_commands.describe(member="Whose balance to check — leave empty for your own")
    @commands.guild_only()
    async def balance(ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        coins = economy.get_balance(ctx.guild.id, member.id)
        whose = "You have" if member.id == ctx.author.id else f"{member.mention} has"
        await ctx.send(embed=_embed(f"{whose} {economy.format_coins(coins)}."))

    @client.hybrid_command(name="donate", aliases=["give"], description="Give some of your coins to someone else in the server")
    @discord.app_commands.describe(member="Who to give coins to", amount="How many coins to give")
    @commands.guild_only()
    async def donate(ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1]):
        if member.id == ctx.author.id:
            await ctx.send(embed=_embed("You can't donate to yourself."), ephemeral=True)
            return
        if member.bot:
            # Nothing could ever spend them -- they'd just vanish.
            await ctx.send(embed=_embed("Bots can't hold coins."), ephemeral=True)
            return
        result = economy.give(ctx.guild.id, ctx.author.id, member.id, amount)
        if result is None:
            coins = economy.get_balance(ctx.guild.id, ctx.author.id)
            await ctx.send(embed=_embed(
                f"You only have {economy.format_coins(coins)} — not enough to give {economy.format_coins(amount)}."
            ), ephemeral=True)
            return
        given, received = result
        # The mention outside the embed pings the receiver so they know.
        await ctx.send(
            content=member.mention,
            embed=_embed(
                f"💸 {ctx.author.mention} gave {member.mention} {economy.format_coins(amount)}!\n"
                f"{ctx.author.display_name}: {economy.format_coins(given)} · "
                f"{member.display_name}: {economy.format_coins(received)}"
            ),
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @client.hybrid_command(name="slots", description="Spin the slot machine")
    @discord.app_commands.describe(bet="How many coins to bet — leave empty to see the payouts")
    @commands.guild_only()
    async def slots(ctx: commands.Context, bet: commands.Range[int, 1] = None):
        if bet is None:
            await ctx.send(embed=_embed(f"🎰 **Slots**\n\n{economy.slots_paytable()}"))
            return
        if not await _take_bet(ctx, bet):
            return
        # Settled before the reels are shown, so the animation can't be
        # interrupted halfway with the money still in limbo.
        reels, multiplier = economy.spin_slots()
        returned = bet * multiplier
        coins = economy.pay(ctx.guild.id, ctx.author.id, returned)

        header = f"🎰 **Slots** — {ctx.author.display_name} bets {economy.format_coins(bet)}"
        hidden = [economy.SLOT_HIDDEN] * 3
        message = await ctx.send(embed=_embed(f"{header}\n\n# {' '.join(hidden)}"))
        for revealed in (1, 2):
            await asyncio.sleep(0.7)
            shown = reels[:revealed] + hidden[revealed:]
            with contextlib.suppress(discord.HTTPException):
                await message.edit(embed=_embed(f"{header}\n\n# {' '.join(shown)}"))
        await asyncio.sleep(0.7)

        if multiplier == 0:
            verdict = "No luck this time."
        elif multiplier == 1:
            verdict = "A pair — your bet comes back."
        else:
            verdict = f"**{multiplier}× payout!**"
        await message.edit(embed=_embed(
            f"{header}\n\n# {' '.join(reels)}\n{verdict}\n{economy.outcome_line(bet, returned, coins)}"
        ))

    @client.hybrid_command(name="roulette", description="Bet on a spin of the roulette wheel")
    @discord.app_commands.describe(
        bet="How many coins to bet",
        space="red, black, odd, even, 1-18, 19-36, 1-12, 13-24, 25-36, green, or a number 0-36",
    )
    @commands.guild_only()
    async def roulette(ctx: commands.Context, bet: commands.Range[int, 1], *, space: str):
        choice = economy.parse_roulette_bet(space)
        if choice is None:
            await ctx.send(embed=_embed(
                f"**{space}** isn't something you can bet on. Try red, black, odd, even, "
                f"1-18, 19-36, 1-12, 13-24, 25-36, green, or a number from 0 to 36."
            ), ephemeral=True)
            return
        if not await _take_bet(ctx, bet):
            return
        number = economy.spin_roulette()
        returned = choice.returned(bet) if number in choice.numbers else 0
        coins = economy.pay(ctx.guild.id, ctx.author.id, returned)

        header = (
            f"🎡 **Roulette** — {ctx.author.display_name} bets "
            f"{economy.format_coins(bet)} on **{choice.label}**"
        )
        message = await ctx.send(embed=_embed(f"{header}\n\nThe ball is spinning..."))
        await asyncio.sleep(2)
        verdict = "✅ **Winner!**" if returned else "❌ **No luck.**"
        await message.edit(embed=_embed(
            f"{header}\n\nThe ball lands on **{economy.roulette_pocket(number)}**\n"
            f"{verdict}\n{economy.outcome_line(bet, returned, coins)}"
        ))

    @roulette.autocomplete('space')
    async def roulette_space_autocomplete(interaction: discord.Interaction, current: str):
        return [
            discord.app_commands.Choice(name=name, value=value)
            for name, value in economy.roulette_suggestions(current)
        ]

    @client.hybrid_command(name="blackjack", description="Play a hand of blackjack against the dealer")
    @discord.app_commands.describe(bet="How many coins to bet")
    @commands.guild_only()
    async def blackjack(ctx: commands.Context, bet: commands.Range[int, 1]):
        if not await _take_bet(ctx, bet):
            return
        view = BlackjackView(economy.BlackjackGame(bet), ctx.guild.id, ctx.author)
        if view.game.finished:
            # A blackjack on the deal: nothing to decide, so no buttons.
            view.settle()
            await ctx.send(embed=view.embed())
            return
        view.message = await ctx.send(embed=view.embed(), view=view)

    @client.hybrid_command(name="poker", description="Play a hand of video poker (Jacks or Better)")
    @discord.app_commands.describe(bet="How many coins to bet — leave empty to see the payouts")
    @commands.guild_only()
    async def poker(ctx: commands.Context, bet: commands.Range[int, 1] = None):
        if bet is None:
            await ctx.send(embed=_embed(f"♠️ **Video Poker** — Jacks or Better\n\n{economy.poker_paytable()}"))
            return
        if not await _take_bet(ctx, bet):
            return
        view = PokerView(economy.PokerGame(bet), ctx.guild.id, ctx.author)
        view.message = await ctx.send(embed=view.embed(), view=view)

    @client.hybrid_command(name="leaderboard", description="The richest people in this server")
    @commands.guild_only()
    async def leaderboard(ctx: commands.Context):
        rows = economy.leaderboard(ctx.guild.id)
        if not rows:
            await ctx.send(embed=_embed("Nobody here has any coins yet — /work to get started."))
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = ["## 🏆 Richest in the server"]
        for place, (user_id, coins) in enumerate(rows):
            rank = medals[place] if place < len(medals) else f"`#{place + 1}`"
            # A mention inside an embed shows the name without pinging anyone.
            lines.append(f"{rank} <@{user_id}> — {economy.format_coins(coins)}")
        await ctx.send(embed=_embed("\n".join(lines)))

    @client.hybrid_command(name="bet", description="Bet on an NFL or soccer game today or tomorrow, before it kicks off")
    @discord.app_commands.describe(
        game="Start typing a team to pick from today's and tomorrow's games",
        pick="Who wins — or a draw, for soccer",
        amount="How many coins to bet",
    )
    @commands.guild_only()
    async def bet(ctx: commands.Context, game: str, pick: str, amount: commands.Range[int, 1]):
        await ctx.defer()  # checking the game with ESPN can take a moment

        def place():
            found = sportsbook.resolve_game(game)
            chosen = sportsbook.resolve_pick(found, pick)
            pays = sportsbook.place_bet(ctx.guild.id, ctx.author.id, ctx.channel.id, found, chosen, amount)
            return found, chosen, pays

        try:
            found, chosen, pays = await asyncio.to_thread(place)
        except sportsbook.BetError as e:
            await ctx.send(embed=_embed(str(e)))
            return
        await ctx.send(embed=_embed(
            f"🎟️ **Bet placed** — {ctx.author.display_name} puts {economy.format_coins(amount)} on "
            f"**{found.pick_name(chosen)}**\n"
            f"{found.describe()}\n"
            f"Pays {economy.format_coins(pays)} if it hits "
            f"({economy.format_multiplier(found.payouts[chosen])}). You'll get pinged here when it ends."
        ))

    @bet.autocomplete('game')
    async def bet_game_autocomplete(interaction: discord.Interaction, current: str):
        suggestions = await asyncio.to_thread(sportsbook.game_suggestions, current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in suggestions]

    @bet.autocomplete('pick')
    async def bet_pick_autocomplete(interaction: discord.Interaction, current: str):
        # Offers the teams of whichever game is already filled in.
        chosen_game = await asyncio.to_thread(sportsbook.cached_game, interaction.namespace.game or "")
        if chosen_game is None:
            return []
        return [
            discord.app_commands.Choice(name=name, value=value)
            for name, value in sportsbook.pick_suggestions(chosen_game)
            if current.casefold() in name.casefold()
        ]

    @client.hybrid_command(name="horsebet", description="Bet on a horse to win a JRA graded stakes race, before post time")
    @discord.app_commands.describe(
        race="Start typing a race to pick from the upcoming JRA card",
        horse="Which horse wins — its name or number",
        amount="How many coins to bet",
    )
    @commands.guild_only()
    async def horsebet(ctx: commands.Context, race: str, horse: str, amount: commands.Range[int, 1]):
        await ctx.defer()  # checking the race and its odds with netkeiba can take a moment

        def place():
            found = jra_bets.resolve_race(race)
            runner, chance = jra_bets.resolve_horse(found, horse)
            payout_pct = jra_bets.place_bet(ctx.guild.id, ctx.author.id, ctx.channel.id, found, runner, chance, amount)
            return found, runner, payout_pct

        try:
            found, runner, payout_pct = await asyncio.to_thread(place)
        except jra_bets.BetError as e:
            await ctx.send(embed=_embed(str(e)))
            return
        await ctx.send(embed=_embed(
            f"🎟️ **Bet placed** — {ctx.author.display_name} puts {economy.format_coins(amount)} on "
            f"**#{runner.number} {runner.name}** to win\n"
            f"{jra_bets.describe(found)}\n"
            f"Pays {economy.format_coins(amount * payout_pct // 100)} if it wins "
            f"({economy.format_multiplier(payout_pct)}). You'll get pinged here when it's run."
        ))

    @horsebet.autocomplete('race')
    async def horsebet_race_autocomplete(interaction: discord.Interaction, current: str):
        choices = await asyncio.to_thread(jra.race_choices, current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in choices]

    @horsebet.autocomplete('horse')
    async def horsebet_horse_autocomplete(interaction: discord.Interaction, current: str):
        # Offers the field of whichever race is already filled in.
        suggestions = await asyncio.to_thread(jra_bets.horse_suggestions, interaction.namespace.race or "", current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in suggestions]

    @client.hybrid_command(name="props", description="An NFL game's prop menu: each team's QB, RB1, WR1 and defense lines")
    @discord.app_commands.describe(game="Start typing a team — leave empty for the next game up")
    async def props_command(ctx: commands.Context, *, game: str | None = None):
        await ctx.defer()
        text = await asyncio.to_thread(nfl_props.menu_text, game)
        await ctx.send(embed=_embed(text))

    @props_command.autocomplete('game')
    async def props_game_autocomplete(interaction: discord.Interaction, current: str):
        suggestions = await asyncio.to_thread(nfl_props.game_suggestions, current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in suggestions]

    @client.hybrid_command(name="propbet", description="Bet the over or under on an NFL QB, RB1, WR1 or defense this week")
    @discord.app_commands.describe(
        game="Start typing a team to pick from this week's games",
        prop="Each team's QB, RB1, WR1 or defense — pick from the menu",
        side="Over or under the line",
        amount="How many coins to bet",
    )
    @commands.guild_only()
    async def propbet(ctx: commands.Context, game: str, prop: str, side: str, amount: commands.Range[int, 1]):
        await ctx.defer()  # building a game's menu from ESPN can take a moment

        def place():
            picked = nfl_props.resolve_side(side)
            found = nfl_props.resolve_game(game)
            # Rebuilt fresh, so the starter and injury checks are current.
            chosen = nfl_props.resolve_prop(found, prop)
            payout_pct = nfl_props.place_bet(ctx.guild.id, ctx.author.id, ctx.channel.id, found, chosen, picked, amount)
            return found, chosen, picked, payout_pct

        try:
            found, chosen, picked, payout_pct = await asyncio.to_thread(place)
        except nfl_props.BetError as e:
            await ctx.send(embed=_embed(str(e)))
            return
        warning = f"\n{chosen.warning}" if chosen.warning else ""
        await ctx.send(embed=_embed(
            f"🎟️ **Bet placed** — {ctx.author.display_name} puts {economy.format_coins(amount)} on "
            f"**{chosen.subject} {picked} {chosen.line:g} {chosen.stat_label}**\n"
            f"{found.describe()}\n"
            f"Pays {economy.format_coins(amount * payout_pct // 100)} if it hits "
            f"({economy.format_multiplier(payout_pct)}). You'll get pinged here when the game ends.{warning}"
        ))

    @propbet.autocomplete('game')
    async def propbet_game_autocomplete(interaction: discord.Interaction, current: str):
        suggestions = await asyncio.to_thread(nfl_props.game_suggestions, current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in suggestions]

    @propbet.autocomplete('prop')
    async def propbet_prop_autocomplete(interaction: discord.Interaction, current: str):
        # Offers the menu of whichever game is already filled in.
        suggestions = await asyncio.to_thread(nfl_props.prop_suggestions, interaction.namespace.game or "", current)
        return [discord.app_commands.Choice(name=name, value=value) for name, value in suggestions]

    @propbet.autocomplete('side')
    async def propbet_side_autocomplete(interaction: discord.Interaction, current: str):
        suggestions = await asyncio.to_thread(
            nfl_props.side_suggestions, interaction.namespace.game or "", interaction.namespace.prop or ""
        )
        return [
            discord.app_commands.Choice(name=name, value=value)
            for name, value in suggestions if current.casefold() in name.casefold()
        ]

    @client.hybrid_command(name="mybets", description="Your sports bets still waiting on a result")
    @commands.guild_only()
    async def mybets(ctx: commands.Context):
        texts = [
            text for text in (
                sportsbook.open_bets_text(ctx.guild.id, ctx.author.id),
                nfl_props.open_bets_text(ctx.guild.id, ctx.author.id),
                jra_bets.open_bets_text(ctx.guild.id, ctx.author.id),
            ) if text
        ]
        if not texts:
            await ctx.send(embed=_embed(
                "You don't have any open bets. /bet to put coins on a game today or tomorrow, "
                "/propbet on an NFL player or defense, or /horsebet on a JRA race."
            ))
            return
        for chunk in _split_by_line("🎟️ **Your open bets**\n" + "\n".join(texts)):
            await ctx.send(embed=_embed(chunk))

    async def economy_error(ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=_embed("Coins belong to a server — use this in one, not a DM."), ephemeral=True)
        elif isinstance(error, commands.RangeError):
            what = "Donations" if ctx.command.name == 'donate' else "Bets"
            await ctx.send(embed=_embed(f"{what} have to be at least 1 coin."), ephemeral=True)
        elif isinstance(error, commands.CommandOnCooldown):
            # Only /mine has a cooldown decorator; /work's cooldown lives in economy.db.
            wait = economy.format_duration(datetime.timedelta(seconds=error.retry_after))
            await ctx.send(embed=_embed(
                f"⛏️ Your pickaxe needs a rest — that's {economy.MINE_LIMIT} mines in "
                f"{economy.MINE_WINDOW_MINUTES} minutes. You can mine again in **{wait}**."
            ), ephemeral=True)
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(
                embed=_embed(f"Usage: `!{ctx.command.name} {ctx.command.signature}`"), ephemeral=True
            )
        else:
            raise error

    for command in (work, mine, balance, donate, slots, roulette, blackjack, poker, bet, propbet, horsebet, mybets, leaderboard):
        command.error(economy_error)

    @client.hybrid_command(name="help", description="Lists every command DJ Shinx offers")
    async def help_command(ctx: commands.Context):
        by_category = {category: [] for category in CATEGORY_ORDER}
        for command in client.commands:
            if command.name in HIDDEN_COMMANDS:
                continue
            if command.name in MUSIC_COMMANDS and (ctx.guild is None or ctx.guild.id != MUSIC_GUILD_ID):
                continue
            by_category.setdefault(COMMAND_CATEGORIES.get(command.name, 'Other'), []).append(command)

        pages = []
        for category, group in by_category.items():
            if not group:
                continue
            lines = [f"## {category} Commands", "*(use with / or !)*"]
            for command in sorted(group, key=lambda c: c.name):
                lines.append(f"`{command.name}` — {command.description or 'No description.'}")
            pages.append((category, "\n".join(lines)))

        view = PaginatorView(pages, author_id=ctx.author.id, command_name="help")
        message = await ctx.send(embed=view.embed(), view=view)
        view.message = message

 #=========================--END COMMANDS--===================================#

    async def _broadcast(feature: str, send):
        """Calls send(channel) for every channel configured for this
        feature (via /setchannel) across every guild the bot is in, so a
        single result gets fanned out to everyone who's opted in instead
        of one hardcoded destination."""
        # Each destination is isolated: an exception escaping a tasks.loop
        # body stops that loop for good, so one server where the bot lost
        # Send Messages/Embed Links (or whose channel was deleted) used to
        # silently kill the feature for every server after it, and for
        # every future tick until a restart.
        for guild_id, channel_id in channel_config.get_all_for_feature(feature).items():
            if feature in MUSIC_FEATURES and guild_id != MUSIC_GUILD_ID:
                continue  # set up before music was limited to its own server
            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except discord.HTTPException as e:
                    logger.warning(
                        f"{feature}: can't reach channel {channel_id} in guild {guild_id} "
                        f"(deleted, the bot isn't in that server, or it lacks View Channel?): {e}"
                    )
                    continue
            try:
                await send(channel)
            except discord.HTTPException as e:
                logger.warning(
                    f"{feature}: failed to post in #{getattr(channel, 'name', channel_id)} "
                    f"(guild {guild_id}) -- check the bot's Send Messages / Embed Links "
                    f"permissions there: {e}"
                )
            except Exception:
                # Anything else is still this destination's problem, not
                # every server's after it.
                logger.exception(f"{feature}: failed to post in channel {channel_id} (guild {guild_id})")

    @tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=EASTERN))
    @_keep_alive
    async def sotd():
        result = await asyncio.to_thread(bf.recsongs)
        await _broadcast('sotd', lambda channel: _send_announcement(channel, result, artwork_as_thumbnail=True))

    @tasks.loop(hours=6.0)
    @_keep_alive
    async def new_chapter_announcements():
        berserk_announcement = await asyncio.to_thread(bf.check_berserk_release)
        batman_announcement = await asyncio.to_thread(bf.check_absolute_batman_release)
        if not berserk_announcement and not batman_announcement:
            return

        async def send(channel):
            if berserk_announcement:
                await _send_announcement(channel, berserk_announcement)
            if batman_announcement:
                await _send_announcement(channel, batman_announcement)

        await _broadcast('manga_comics', send)

    @tasks.loop(minutes=15.0)
    @_keep_alive
    async def f1_updates():
        messages = await asyncio.to_thread(f1.check_f1_updates)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await channel.send(embed=_embed(message))

        await _broadcast('f1_updates', send)

    @tasks.loop(minutes=5.0)
    @_keep_alive
    async def jra_updates():
        # Every 5 minutes rather than F1's 15, so the "minutes to post"
        # alert lands close to half an hour before the off.
        messages = await asyncio.to_thread(jra.check_jra_updates)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await channel.send(embed=_embed(message))

        await _broadcast('jra_updates', send)

    @tasks.loop(minutes=30.0)
    @_keep_alive
    async def game_announcements():
        messages = await asyncio.to_thread(game_news.check_game_announcements)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await _send_announcement(channel, message)

        await _broadcast('game_announcements', send)

    @tasks.loop(minutes=5.0)
    @_keep_alive
    async def game_reminders():
        # Runs more often than the other game_news loops so a "30 minutes
        # before" reminder actually lands close to 30 minutes out, not
        # off by up to half an hour.
        due = await asyncio.to_thread(game_news.due_reminders)
        if not due:
            return

        async def send(channel):
            for reminder in due:
                heading = "Airing in 30 minutes!" if reminder['kind'] == 'thirty_min' else "Airing tomorrow!"
                message = f"⏰ **{heading}**\n**{reminder['title']}** — {reminder['display_time']}\n{reminder['url']}"
                await _send_announcement(channel, message)

        await _broadcast('game_announcements', send)

    @tasks.loop(minutes=30.0)
    @_keep_alive
    async def game_recaps():
        messages = await asyncio.to_thread(game_news.check_recaps)
        if not messages:
            return

        async def send(channel):
            for message in messages:
                await _send_announcement(channel, message)

        await _broadcast('game_announcements', send)

    @tasks.loop(minutes=30.0)
    @_keep_alive
    async def steam_sale_alerts():
        # Events first: a franchise or seasonal sale is the context for
        # whatever individual discounts follow it in the same batch.
        # Shares the steam_sales channel opt-in rather than adding a
        # separate /setchannel feature -- anyone who wants sale alerts
        # wants to know a sale event has started.
        events = await asyncio.to_thread(steam_sales.check_sale_events)
        messages = await asyncio.to_thread(steam_sales.check_steam_sales)
        if not events and not messages:
            return

        async def send(channel):
            for message in events + messages:
                await _send_announcement(channel, message)

        await _broadcast('steam_sales', send)

    @tasks.loop(minutes=5.0)
    @_keep_alive
    async def nfl_prop_menus():
        # Rebuilds every game's /propbet menu in the background: building
        # one cold takes ESPN a few seconds, and Discord only gives an
        # autocomplete 3. warm_menus never raises, so this loop can't die.
        await asyncio.to_thread(nfl_props.warm_menus)

    @tasks.loop(minutes=5.0)
    @_keep_alive
    async def sports_bet_results():
        # Pings each bettor in the channel they bet from, not a /setchannel
        # destination -- a bet result is personal. The coins are already
        # paid by the time this posts, so a send that fails only loses
        # the message.
        settlements = []
        for settle in (sportsbook.settle_finished_bets, nfl_props.settle_finished_bets, jra_bets.settle_finished_bets):
            try:
                settlements += await asyncio.to_thread(settle)
            except Exception as e:
                # Escaping here would stop the loop for good, leaving every
                # later bet unsettled until a restart -- and each book is
                # tried on its own, so one failing can't hold up the other.
                logger.exception(f"{settle.__module__}: settling bets failed: {e}")

        for settlement in settlements:
            try:
                channel = client.get_channel(settlement.channel_id) or await client.fetch_channel(settlement.channel_id)
                await channel.send(
                    content=f"<@{settlement.user_id}>",
                    embed=_embed(f"🎟️ **Bet settled**\n{settlement.text}"),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException as e:
                logger.warning(
                    f"sportsbook: couldn't post a bet result for user {settlement.user_id} "
                    f"in channel {settlement.channel_id}: {e}"
                )

    async def _post_update_log():
        """Drops a short changelog in the maintainer channel when the bot
        comes back up on new code. Sends straight to one hardcoded channel
        rather than through _broadcast, so this can't reach any other
        server, and there's no command that exposes it. Never raises: a
        tracking nicety must not be able to take startup down with it."""
        try:
            summary = await asyncio.to_thread(update_log.pending_update)
            if not summary:
                return

            channel = client.get_channel(UPDATE_LOG_CHANNEL_ID)
            if channel is None:
                # Not in that guild, or the channel is gone. update_log has
                # already advanced its baseline, so this won't be retried --
                # log it locally so the changelog isn't just lost silently.
                logger.warning(
                    f"update_log: channel {UPDATE_LOG_CHANNEL_ID} not reachable; "
                    f"update went unposted:\n{summary}"
                )
                return

            for chunk in llmask.chunk_response(summary):
                await channel.send(embed=_embed(chunk))
        except Exception as e:
            logger.warning(f"update_log: couldn't post the update summary: {e}")

    @client.event
    async def on_ready():
        # on_ready fires again whenever the gateway has to start a fresh
        # session, and starting a loop that's already running raises --
        # which aborted this handler on every reconnect after the first.
        # A loop that has stopped is started again.
        loops = (
            sotd, new_chapter_announcements, f1_updates, jra_updates, game_announcements,
            game_reminders, game_recaps, steam_sale_alerts, sports_bet_results, nfl_prop_menus,
        )
        for loop in loops:
            if not loop.is_running():
                loop.start()
        logger.info(f'{client.user} is now running!')
        await client.tree.sync()
        try:
            # The music commands are registered to their own server only,
            # which a global sync doesn't cover.
            await client.tree.sync(guild=discord.Object(id=MUSIC_GUILD_ID))
        except discord.HTTPException as e:
            logger.warning(f"Couldn't sync the music commands to server {MUSIC_GUILD_ID}: {e}")
        await _post_update_log()

    @client.event
    async def on_guild_join(guild: discord.Guild):
        # A server can set up F1 updates before the bot is really in it --
        # its slash commands work without that -- and on race day the day's
        # announcement has gone out to everyone else by the time it joins.
        await _f1_race_day_catch_up(client, guild.id)

    client.run(TOKEN)


if __name__ == '__main__':
    run_discord_bot()