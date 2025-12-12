from __future__ import annotations
import os
from pathlib import Path
from typing import Dict, Optional, List
import random
import itertools
import asyncio
import math
from io import BytesIO
from PIL import Image

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from game import Table, Stage
import storage


# ============================================================
#                      CONFIG & PATHS
# ============================================================

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")

# optional forced host via .env
HOST_OVERRIDE = os.getenv("HOST_USER_ID")
if HOST_OVERRIDE:
    HOST_OVERRIDE = int(HOST_OVERRIDE)

ROOT_DIR = Path(__file__).parent

CARDS_DIR = ROOT_DIR / "assets" / "cards"
FRONT_DIR = CARDS_DIR / "front"
BACK_PATH = CARDS_DIR / "cards_back.png"

CHIP_PATH = ROOT_DIR / "assets" / "chips" / "bmt_chip.png"
WELCOME_PATH = ROOT_DIR / "assets" / "ui" / "welcome.png"
TABLE_VIEW_IMAGE = ROOT_DIR / "assets" / "ui" / "table_view.png"

TABLE_VIEW_VIDEO_PATH = ROOT_DIR / "assets" / "ui" / "table_view.mp4"
WINNER_VIDEO_PATH = ROOT_DIR / "assets" / "ui" / "winner.mp4"
WINNER_PATH = ROOT_DIR / "assets" / "ui" / "winner.png"


# ============================================================
#                    CARD IMAGE UTILITIES
# ============================================================

def card_path(code: str) -> Path:
    return FRONT_DIR / f"{code}.png"


def build_cards_sprite(cards: list[str], per_row: int = 2) -> Optional[BytesIO]:
    """Builds one combined PNG sprite from list of card images."""
    images = []
    for code in cards:
        p = card_path(code)
        if not p.exists():
            continue
        img = Image.open(p).convert("RGBA")
        images.append(img)

    if not images:
        return None

    w, h = images[0].size
    cols = min(per_row, len(images))
    rows = math.ceil(len(images) / per_row)

    sprite = Image.new("RGBA", (cols * w, rows * h), (0, 0, 0, 0))

    for i, img in enumerate(images):
        r = i // per_row
        c = i % per_row
        sprite.paste(img, (c * w, r * h), img)

    buf = BytesIO()
    sprite.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ============================================================
#                 TABLE STORAGE (PER TOPIC)
# ============================================================

TABLES: Dict[tuple[int, Optional[int]], Table] = {}


def get_table(update: Update, create: bool = False) -> Optional[Table]:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type not in ("group", "supergroup"):
        return None

    thread_id = msg.message_thread_id
    key = (chat.id, thread_id)

    table = TABLES.get(key)
    if not table and create:
        table = Table(chat_id=chat.id, thread_id=thread_id)
        TABLES[key] = table

    return table


# ============================================================
#                      MENUS
# ============================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎛 Poker Session Setup", callback_data="setup_menu")],
            [
                InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
                InlineKeyboardButton("📊 Table Stats", callback_data="show_stats"),
            ],
            [InlineKeyboardButton("📜 Rules & Commands", callback_data="show_rules")],
        ]
    )


def setup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚙️ Chips", callback_data="cfg_chips"),
                InlineKeyboardButton("🎯 Rounds", callback_data="cfg_rounds"),
            ],
            [
                InlineKeyboardButton("👥 Players", callback_data="cfg_players"),
            ],
            [InlineKeyboardButton("ℹ️ Setup Guide", callback_data="setup_help")],
            [InlineKeyboardButton("♻ Reset Table", callback_data="reset_table")],
            [InlineKeyboardButton("🎬 Start Poker Session", callback_data="start_session")],
            [InlineKeyboardButton("⬅ Back to Main Menu", callback_data="back_main")],
        ]
    )


def build_action_keyboard() -> InlineKeyboardMarkup:
    """Buttons shown during a hand."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Check/Call", callback_data="act:call"),
                InlineKeyboardButton("💸 Raise", callback_data="act:raise_menu"),
                InlineKeyboardButton("🏳️ Fold", callback_data="act:fold"),
            ],
            [
                InlineKeyboardButton("🂷 Show Board", callback_data="show_board"),
                InlineKeyboardButton("👀 View Table", callback_data="view_round"),
            ],
        ]
    )


def build_raise_menu() -> InlineKeyboardMarkup:
    """
    Raise menu with fixed + custom + ALL-IN.
    Beträge passen jetzt zu den hohen Stacks (z.B. 250k+ Startchips)
    und die angezeigten Zahlen stimmen mit dem echten Raise überein.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Raise 10 000", callback_data="raiseamt:10000"),
                InlineKeyboardButton("Raise 50 000", callback_data="raiseamt:50000"),
            ],
            [
                InlineKeyboardButton("Raise 100 000", callback_data="raiseamt:100000"),
                InlineKeyboardButton("Raise 250 000", callback_data="raiseamt:250000"),
            ],
            [InlineKeyboardButton("ALL-IN", callback_data="raiseamt:all")],
            [
                InlineKeyboardButton("✏ Custom", callback_data="raise_custom"),
                InlineKeyboardButton("⬅ Back", callback_data="back_actions"),
            ],
        ]
    )


# ============================================================
#                      START / RULES COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    # /start in group -> DM redirect
    if chat.type != "private":
        await msg.reply_text(
            "👋 DM me with /start for a short intro.\n"
            "In the group I only listen to buttons and poker drama."
        )
        return

    text = (
        "🃏 *BMT Texas Hold'em Bot*\n\n"
        "• Join the table in the group topic\n"
        "• Hole cards are sent privately\n"
        "• Bet using buttons\n"
        "• Chips = BMT (Bitcoin Maxi Tears) 💧\n"
        "• Pure fun — no real stakes\n\n"
        "⚠️ *Disclaimer:*\n"
        "This game is *not gambling* and no real money is involved.\n"
        "All chips, points, and rewards have *no real-world value* and exist solely for entertainment.\n"
        "The bot does *not* offer deposits, withdrawals, or any real-money transactions.\n\n"
        "Go back to the topic to begin!"
    )
    await msg.reply_text(text, parse_mode="Markdown")



async def send_rules(chat_id: int, thread_id: Optional[int], context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📜 *BMT Texas Hold'em – Quick Guide*\n\n"
        "• 2 hole cards per player\n"
        "• 5 community cards\n"
        "• Best 5-card poker hand wins\n\n"
        "Buttons during hand:\n"
        "• Check/Call, Raise, Fold\n"
        "• Show Board, View Table\n\n"
        "Menu:\n"
        "• Poker Session Setup\n"
        "• Leaderboard\n"
        "• Table Stats\n"
        "• Reset Table\n"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        message_thread_id=thread_id,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="back_main")]]
        ),
    )


async def send_leaderboard(chat_id: int, thread_id: int, context):
    """Send leaderboard and auto-delete after 10 seconds."""
    try:
        rows = storage.get_leaderboard()

        if not rows:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="🏆 No leaderboard yet.",
                message_thread_id=thread_id
            )
            await asyncio.sleep(10)
            await msg.delete()
            return

        lines = ["🏆 BMT Leaderboard\n"]
        for i, row in enumerate(rows, start=1):
            name = row["name"]
            won = row["total_chips_won"]
            played = row["hands_played"]
            hw = row["hands_won"]
            lines.append(
                f"{i}. {name} – 💰 {won} BMT (🃏 {hw}/{played} hands won)"
            )

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            message_thread_id=thread_id
        )

        await asyncio.sleep(10)
        await msg.delete()

    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Leaderboard error: {e}",
            message_thread_id=thread_id
        )


async def send_table_stats(chat_id: int, thread_id: int, table, context):
    """Send table stats and auto-delete after 10 seconds."""
    try:
        if not table or not table.players:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="📊 No players at the table.",
                message_thread_id=thread_id
            )
            await asyncio.sleep(10)
            await msg.delete()
            return

        lines = []
        lines.append("📊 Table Stats")
        lines.append("")
        lines.append(f"• Host: {table.host_id}")
        lines.append(f"• Players: {len(table.players)}")
        lines.append(f"• Stage: {table.stage.name}")
        lines.append(f"• Pot: {table.pot} BMT")
        comm = ", ".join(table.community_cards) if table.community_cards else "—"
        lines.append(f"• Community: {comm}")
        lines.append("")
        lines.append("Players:")

        for p in table.players.values():
            lines.append(f"• {p.name} – {p.chips} BMT ")

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            message_thread_id=thread_id
        )

        await asyncio.sleep(10)
        await msg.delete()

    except Exception as e:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Table stats error: {e}",
            message_thread_id=thread_id
        )


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    await send_rules(chat.id, msg.message_thread_id, context)

async def fullreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Voller Reset:
    - Alle aktiven Tische aus dem RAM löschen
    - Alle Leaderboard-Stats in der DB zurücksetzen
    Nur der Host darf das aus einem Topic heraus machen.
    """
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    # 1) Alle Tische zurücksetzen (global)
    TABLES.clear()

    # 2) Leaderboard / Stats in der DB zurücksetzen
    try:
        storage.reset_all_stats()
        await msg.reply_text(
            "♻ Full reset complete.\n"
            "• All tables cleared\n"
            "• Leaderboard stats set back to zero\n\n"
            "You all start fresh. Nobody is the chip king anymore. 💧"
        )
    except Exception as e:
        await msg.reply_text(f"⚠️ Error during full reset: {e!r}")



async def settable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Use /settable inside a group topic.")
        return

    if msg.message_thread_id is None:
        await msg.reply_text(
            "Please run /settable *inside a topic*, not the main chat.",
            parse_mode="Markdown",
        )
        return

    table = get_table(update, create=True)
    table.host_id = HOST_OVERRIDE if HOST_OVERRIDE else user.id

    # Welcome graphic
    if WELCOME_PATH.exists():
        with open(WELCOME_PATH, "rb") as f:
            await msg.reply_photo(
                InputFile(f),
                caption="🃏 Welcome to *BMT Texas Hold'em*!",
                parse_mode="Markdown",
            )

    await msg.reply_text(
        "This topic is now a poker table.\n"
        "Use the menu below:",
        reply_markup=main_menu_keyboard(),
    )


async def auto_delete_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay: int = 20,
):
    """Delete a message after <delay> seconds, ignoring errors."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


AFK_WARNING_SECONDS = 120     # 2 minutes → send funny warning
AFK_KICK_SECONDS = 300        # 5 minutes → auto-fold & remove player


async def afk_watcher_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Wird regelmäßig vom JobQueue aufgerufen.
    Warnt nach 2 Minuten und foldet nach 5 Minuten AFK-Spieler.
    Die BMT-Chips bleiben im Pot.
    Wenn danach nur noch 1 Spieler übrig ist, gewinnt der automatisch den Pot.
    """
    now = asyncio.get_event_loop().time()
    app = context.application

    # Alle Tische durchgehen
    for (chat_id, thread_id), table in list(TABLES.items()):
        changed = False  # hat sich am Tisch etwas getan (Fold)?

        for p in list(table.players.values()):
            # Bereits gefoldete Spieler nicht prüfen
            if getattr(p, "folded", False):
                continue

            # Kein Timestamp → z.B. gerade erst gejoint
            last = getattr(p, "last_action_time", None)
            if last is None:
                continue

            diff = now - last

            # 1) Kick nach 5 Minuten hat Vorrang
            if diff >= AFK_KICK_SECONDS:
                table.fold(p.user_id)   # Chips bleiben im Pot
                changed = True
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=(
                            f"💤 {p.name} has been AFK too long and got folded.\n"
                            "Those Tears stay in the pot — thanks for the donation! 💧💰"
                        ),
                    )
                except Exception:
                    pass

            # 2) Warnung nach 2 Minuten (nur, wenn noch nicht gekickt)
            elif diff >= AFK_WARNING_SECONDS and not getattr(p, "afk_warned", False):
                p.afk_warned = True
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=(
                            f"😴 {p.name} is falling asleep at the table...\n"
                            "Maxi, click something or I'll fold you like a cheap lawn chair!"
                        ),
                    )
                except Exception:
                    pass

        # Wenn sich was geändert hat (mindestens ein Fold)
        if changed:
            # Aktive Spieler (nicht gefoldet)
            active = [pl for pl in table.players.values() if not pl.folded]

            # FALL 1: Nur noch 1 Spieler übrig → Auto-Gewinner (auch bei Pot = 0)
            if len(active) == 1:
                winner = active[0]
                pot_amount = table.pot

                # Pot dem Gewinner geben (kann 0 sein)
                winner.chips += pot_amount

                # Stats speichern (Gewinner + Verlierer)
                try:
                    storage.record_hand_result(winner.user_id, pot_amount, True)
                    for pl in table.players.values():
                        if pl.user_id != winner.user_id:
                            storage.record_hand_result(pl.user_id, 0, False)
                except Exception:
                    pass

                # Tisch auf Showdown setzen und Pot leeren
                table.stage = Stage.SHOWDOWN
                table.pot = 0

                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🎬 New Hand", callback_data="start_hand")],
                        [
                            InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
                            InlineKeyboardButton("📊 Table Stats", callback_data="show_stats"),
                        ],
                    ]
                )

                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=(
                            "🃏 Showdown (AFK Edition)!\n\n"
                            "Everyone else disappeared into maxi-land.\n"
                            f"🏆 {winner.name} wins the pot by default.\n\n"
                            "Hit New Hand to keep the Tears flowing."
                        ),
                        reply_markup=kb,
                    )
                except Exception:
                    pass

            # FALL 2: Es sind noch mehrere aktiv → normal Street-Logik
            else:
                table.advance_stage_if_needed()


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    await send_leaderboard(chat.id, msg.message_thread_id, context)

async def fullreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Full Reset:
    - löscht das Table-Objekt für dieses Topic
    - löscht das komplette Leaderboard (DB-Tabelle players leeren)
    Nur vom Host nutzbar.
    """
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Use /fullreset inside the poker group/topic.")
        return

    table = get_table(update, create=False)
    if not table:
        await msg.reply_text("No active table found for this topic.")
        return

    # Host-Check
    if table.host_id not in (None, user.id) and (HOST_OVERRIDE is None or user.id != HOST_OVERRIDE):
        await msg.reply_text("🚫 Only the current table host can use /fullreset.")
        return

    # Table-Objekt löschen
    key = (chat.id, msg.message_thread_id)
    if key in TABLES:
        del TABLES[key]

    # Leaderboard/Stats zurücksetzen
    try:
        storage.reset_stats()
    except Exception as e:
        await msg.reply_text(f"⚠️ Full reset failed: {e!r}")
        return

    await msg.reply_text(
        "♻ Full reset done.\n"
        "Table & leaderboard have been wiped. Fresh Tears for everyone."
    )


# ============================================================
#                      BUTTON HANDLER
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    msg = query.message

    # CallbackQuery kann "zu alt" sein → sauber abfangen
    try:
        await query.answer()
    except BadRequest:
        # Query zu alt / schon beantwortet – ignorieren
        return

    # ---------------------------------------------------------
    #  TABLE HOLEN
    # ---------------------------------------------------------
    table = get_table(update, create=True)
    thread_id = msg.message_thread_id

    # AFK-Timestamp für aktive Spieler aktualisieren
    now = asyncio.get_event_loop().time()
    if user.id in table.players:
        setattr(table.players[user.id], "last_action_time", now)

    # ---------------------------------------------------------
    #  HOST-CHECK
    # ---------------------------------------------------------
    host_only_prefixes = (
        "cfg_chips",
        "cfg_rounds",
        "cfg_players",
        "set_chips:",
        "set_hands:",
        "set_players:",
        "reset_table",
        "setup_menu",
        "setup_help",
        "start_session",
        "start_hand",
    )

    if any(data.startswith(p) for p in host_only_prefixes):
        # Falls noch kein Host gesetzt → dieser User wird Host
        if table.host_id is None:
            table.host_id = user.id

        if user.id != table.host_id:
            await query.message.reply_text(
                "🚫 Only the table host can change the settings, maxi.\n"
                "Democracy ends where poker begins."
            )
            return

    # ---------------------------------------------------------
    #  GENERISCHE MENÜ-BUTTONS
    # ---------------------------------------------------------

    if data == "back_main":
        await query.message.reply_text(
            "⬅ Back to main table menu.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "show_rules":
        await send_rules(msg.chat_id, thread_id, context)
        return

    if data == "show_leaderboard":
        await send_leaderboard(msg.chat_id, thread_id, context)
        return

    if data == "show_stats":
        await send_table_stats(msg.chat_id, thread_id, table, context)
        return

    if data == "show_board":
        # Board-Bild schicken und nach 25s wieder löschen (Preview)
        await send_board_images_to_topic(
            chat_id=msg.chat_id,
            table=table,
            context=context,
            explain=True,
            auto_delete=True,
        )
        return

    # ---------------------------------------------------------
    #  SETUP-MENÜ
    # ---------------------------------------------------------

    if data == "setup_menu":
        await query.message.reply_text(
            "🎛 *Poker Session Setup*\n\n"
            "Configure everything before starting the match:",
            parse_mode="Markdown",
            reply_markup=setup_menu_keyboard(),
        )
        return

    if data == "setup_help":
        await query.message.reply_text(
            "ℹ️ *Setup Guide*\n\n"
            "• Chips = Starting stacks\n"
            "• Rounds = Number of hands\n"
            "• Players = Max seats\n",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅ Back", callback_data="setup_menu")]]
            ),
        )
        return

    # ---------------------------------------------------------
    #  RESET TABLE (nur dieses Topic, nicht Leaderboard)
    # ---------------------------------------------------------

    if data == "reset_table":
        key = (msg.chat_id, thread_id)
        if key in TABLES:
            del TABLES[key]
        await query.message.reply_text(
            "♻ Table has been reset.\nUse /settable to start a new match."
        )
        return

    # ---------------------------------------------------------
    #  START SESSION
    # ---------------------------------------------------------

    if data == "start_session":
        table.hands_played = 0  # Reset Hands-Zähler

        max_hands_txt = "∞ (free play)" if table.max_hands == 0 else f"{table.max_hands} hands"
        max_players_txt = (
            "unlimited" if table.max_players == 0 else f"{table.max_players} players"
        )

        session_kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧑‍💻 Join Table", callback_data="join_table")],
                [InlineKeyboardButton("🎬 Start Hand", callback_data="start_hand")],
            ]
        )

        await query.message.reply_text(
            "🎬 *Poker Session is live!*\n\n"
            f"Host: {user.mention_html()}\n\n"
            "*Settings:*\n"
            f"• Starting stack: {table.starting_chips} BMT\n"
            f"• Hands: {max_hands_txt}\n"
            f"• Seats: {max_players_txt}\n\n"
            "Players: press *Join Table*.\n"
            "Host: press *Start Hand* when players are ready.",
            parse_mode="HTML",
            reply_markup=session_kb,
        )
        return

    # ---------------------------------------------------------
    #  JOIN TABLE
    # ---------------------------------------------------------

    if data == "join_table":
        # Schon am Tisch?
        if user.id in table.players:
            await query.message.reply_text("🪑 You're already seated at this table.")
            return

        max_p = getattr(table, "max_players", 0)
        if max_p and len(table.players) >= max_p:
            await query.message.reply_text("🚫 Table is full.")
            return

        name = user.first_name or user.username or "Unnamed maxi"
        table.add_player(user.id, name)
        storage.ensure_player(user.id, name)
        setattr(table.players[user.id], "last_action_time", now)

        players_txt = ", ".join(p.name for p in table.players.values())

        join_start_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🧑‍💻 Join Table", callback_data="join_table"),
                    InlineKeyboardButton("🎬 Start Hand", callback_data="start_hand"),
                ]
            ]
        )

        # Bestätigung mit Buttons
        await query.message.reply_text(
            f"💺 {user.mention_html()} joined the table.\n"
            f"Players now: {players_txt}",
            parse_mode="HTML",
            reply_markup=join_start_kb,
        )

        # Chip-Bild
        if CHIP_PATH.exists():
            with open(CHIP_PATH, "rb") as f:
                await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=InputFile(f),
                    caption=f"💰 {name} sits down with {table.starting_chips} BMT.",
                    message_thread_id=thread_id,
                )

        # „Who’s next?“ mit denselben Buttons
        await context.bot.send_message(
            chat_id=msg.chat_id,
            message_thread_id=thread_id,
            text="Who’s next? Hit *Join Table*! 💺\nHost can hit *Start Hand* anytime.",
            parse_mode="Markdown",
            reply_markup=join_start_kb,
        )
        return

    # ---------------------------------------------------------
    #  CHIP / ROUND / PLAYER CONFIG
    # ---------------------------------------------------------

    if data == "cfg_chips":
        await query.message.reply_text(
            "⚙️ Starting chips:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("250000", callback_data="set_chips:250000"),
                        InlineKeyboardButton("500000", callback_data="set_chips:500000"),
                    ],
                    [
                        InlineKeyboardButton("1000000", callback_data="set_chips:1000000"),
                        InlineKeyboardButton("2500000", callback_data="set_chips:2500000"),
                    ],
                    [InlineKeyboardButton("⬅ Back", callback_data="setup_menu")],
                ]
            ),
        )
        return

    if data.startswith("set_chips:"):
        value = int(data.split(":", 1)[1])
        table.starting_chips = value
        await query.message.reply_text(
            f"✅ Starting stack set to {value} BMT.",
            reply_markup=setup_menu_keyboard(),
        )
        return

    if data == "cfg_rounds":
        await query.message.reply_text(
            "🎯 Select number of hands:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("5", callback_data="set_hands:5"),
                        InlineKeyboardButton("10", callback_data="set_hands:10"),
                    ],
                    [
                        InlineKeyboardButton("20", callback_data="set_hands:20"),
                        InlineKeyboardButton("∞ Free Play", callback_data="set_hands:0"),
                    ],
                    [InlineKeyboardButton("⬅ Back", callback_data="setup_menu")],
                ]
            ),
        )
        return

    if data.startswith("set_hands:"):
        value = int(data.split(":", 1)[1])
        table.max_hands = value
        await query.message.reply_text(
            f"✅ Hands set to {'∞' if value == 0 else value}.",
            reply_markup=setup_menu_keyboard(),
        )
        return

    if data == "cfg_players":
        await query.message.reply_text(
            "👥 Select max seats:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("2", callback_data="set_players:2"),
                        InlineKeyboardButton("3", callback_data="set_players:3"),
                        InlineKeyboardButton("4", callback_data="set_players:4"),
                    ],
                    [
                        InlineKeyboardButton("6", callback_data="set_players:6"),
                        InlineKeyboardButton("8", callback_data="set_players:8"),
                        InlineKeyboardButton("Unlimited", callback_data="set_players:0"),
                    ],
                    [InlineKeyboardButton("⬅ Back", callback_data="setup_menu")],
                ]
            ),
        )
        return

    if data.startswith("set_players:"):
        value = int(data.split(":", 1)[1])
        table.max_players = value
        await query.message.reply_text(
            f"✅ Max seats set to {value if value != 0 else 'unlimited'}.",
            reply_markup=setup_menu_keyboard(),
        )
        return

    # ---------------------------------------------------------
    #  RAISE-MENÜ + HANDLING
    # ---------------------------------------------------------

    if data == "act:raise_menu":
        # Nur der aktuelle Spieler darf das Raise-Menü öffnen
        current_id = table.current_player_id()
        if current_id is None or current_id != user.id:
            await query.message.reply_text("⏳ Not your turn.")
            return

        p = table.players.get(user.id)
        stack_info = f"\n\nYou currently have *{p.chips}* BMT left." if p else ""

        await query.message.reply_text(
            "🎯 *Select your raise amount:*" + stack_info,
            parse_mode="Markdown",
            reply_markup=build_raise_menu(),
        )
        return

    if data == "back_actions":
        await query.message.reply_text(
            "➡ Back to actions.",
            reply_markup=build_action_keyboard(),
        )
        return

    if data.startswith("raiseamt:"):
        amt = data.split(":", 1)[1]

        if amt.lower() in ("all", "allin", "all-in"):
            await handle_action(query, table, "raise_allin", context)
            return

        try:
            value = int(amt)
        except Exception:
            await query.message.reply_text("❌ Invalid raise amount.")
            return

        await handle_action(query, table, f"raise_{value}", context)
        return

    if data == "raise_custom":
        context.user_data["awaiting_custom_raise"] = True
        await query.message.reply_text(
            "💬 Type your raise amount:\nExample: `150000`",
            parse_mode="Markdown",
        )
        return

    # ---------------------------------------------------------
    #  START HAND
    # ---------------------------------------------------------

    if data == "start_hand":
        await handle_start_hand(query, table, context)
        return

    # ---------------------------------------------------------
    #  ACTIONS: CALL / RAISE / FOLD
    # ---------------------------------------------------------

    if data.startswith("act:"):
        if user.id not in table.players:
            await query.message.reply_text("🚫 You're not seated.")
            return

        current_id = table.current_player_id()
        if current_id is not None and current_id != user.id:
            await query.message.reply_text("⏳ Not your turn.")
            return

        action = data.split(":", 1)[1]
        await handle_action(query, table, action, context)
        return

    # ---------------------------------------------------------
    #  VIEW TABLE (VIDEO mit Auto-Delete)
    # ---------------------------------------------------------

    if data == "view_round":
        msg_obj = None

        if TABLE_VIEW_VIDEO_PATH.exists():
            with open(TABLE_VIEW_VIDEO_PATH, "rb") as vid:
                msg_obj = await context.bot.send_video(
                    chat_id=msg.chat_id,
                    video=InputFile(vid),
                    caption="👀 A quick look at the chaos...",
                    message_thread_id=thread_id,
                )
        elif TABLE_VIEW_IMAGE.exists():
            with open(TABLE_VIEW_IMAGE, "rb") as f:
                msg_obj = await context.bot.send_photo(
                    chat_id=msg.chat_id,
                    photo=InputFile(f),
                    caption="👀 A quick look at the chaos...",
                    message_thread_id=thread_id,
                )

        if msg_obj and context.application:
            context.application.create_task(
                auto_delete_message(context, msg.chat_id, msg_obj.message_id, delay=25)
            )
        return
# ============== GAME FLOW HELPERS ==============

async def handle_start_hand(query, table: Table, context: ContextTypes.DEFAULT_TYPE):
    """
    Start a new hand, unless match is already finished.
    Sends:
    - DM hole cards to humans (wenn möglich)
    - Group status with action buttons (Check/Call, Raise, Fold)
    """
    try:
        chat_id = query.message.chat_id

        # Match schon durch?
        if table.max_hands and table.hands_played >= table.max_hands:
            await show_match_finished(query, table, context)
            return

        # Mindestens 1 Spieler
        if len(table.players) < 1:
            await query.message.reply_text(
                "You need at least one player. Even Bitcoin maxis can manage that."
            )
            return

        # Hand-Zähler erhöhen
        table.hands_played += 1

        # Neue Hand vorbereiten
        table.reset_for_new_hand()
        table.deal_hole_cards()

        # AFK-Status für alle Spieler zurücksetzen
        now = asyncio.get_event_loop().time()
        for pl in table.players.values():
            pl.last_action_time = now
            pl.afk_warned = False

        # Hole Cards an User senden (sofern möglich)
        for p in table.players.values():
            if len(p.hole_cards) != 2:
                continue

            try:
                # Direkt user_id als chat_id nutzen
                await send_card_images_to_player(p.user_id, p.hole_cards, context)
            except Forbidden:
                # Spieler hat Bot nie privat gestartet → Hinweis in der Gruppe
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=table.thread_id,
                        text=(
                            f"⚠️ {p.name}, I can't DM you your cards.\n"
                            "Please open a private chat with me and send /start first."
                        ),
                    )
                except Exception:
                    pass
            except Exception:
                # Sonstige Fehler ignorieren, Hand trotzdem weiterlaufen lassen
                pass

        total_hands = "∞" if table.max_hands == 0 else str(table.max_hands)

        # Aktuellen Spieler bestimmen
        first_id = table.current_player_id()
        if first_id is not None and first_id in table.players:
            first_name = table.players[first_id].name
            next_line = (
                f"➡ First to act: {first_name}\n"
                f"{first_name}, try not to punt your whole stack on the very first click. 💧"
            )
        else:
            next_line = "➡ First to act: unknown chaos."

        text = (
            "🃏 *New hand started!*\n"
            f"Hand: *{table.hands_played}/{total_hands}*\n"
            f"Players seated: {len(table.players)}\n"
            "Hole cards have been sent privately (for humans that DM'd me).\n\n"
            "The Tears are cold, the maxis are bold – have fun and good luck. 🍀💧\n\n"
            f"{next_line}"
        )

        kb = build_action_keyboard()

        # Optional: Chip-Bild posten (wenn vorhanden)
        if CHIP_PATH.exists():
            try:
                with open(CHIP_PATH, "rb") as f:
                    await query.message.reply_photo(
                        InputFile(f),
                        caption="💰 Fresh stack, fresh pain incoming.",
                        message_thread_id=table.thread_id,
                    )
            except Exception:
                # Nur kosmetisch, kein Grund abzubrechen
                pass

        # Status-Text mit Action-Buttons
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
            parse_mode="Markdown",      # nur statische Teile sind fett, keine Usernamen
            message_thread_id=table.thread_id,
        )

    except Exception as e:
        # Fehler direkt in der Gruppe anzeigen
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⚠️ Error in handle_start_hand: {e!r}",
            message_thread_id=table.thread_id,
        )


async def handle_action(query, table: Table, action: str, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle user actions (call / raise / fold / all-in),
    auto-advance streets, and do showdown if needed.
    (Keine Bots mehr – nur echte Maxis.)
    """
    try:
        user = query.from_user
        p = table.players.get(user.id)

        # Spieler sitzt gar nicht am Tisch
        if not p:
            await query.message.reply_text("You're not even seated. Spectating is free though.")
            return

        # Spieler ist bereits gefoldet → darf nichts mehr machen
        if getattr(p, "folded", False):
            await query.message.reply_text(
                "🏳️ You're already folded.\n"
                "Grab some popcorn and wait for the next hand."
            )
            return

        # Ist der Spieler überhaupt am Zug?
        current_id = table.current_player_id()
        if current_id is not None and current_id != user.id:
            await query.message.reply_text("⏳ Not your turn.")
            return

        # Hand bereits vorbei?
        if table.stage == Stage.SHOWDOWN:
            await query.message.reply_text(
                "🃏 This hand is already over.\n"
                "Hit *New Hand* or start a new session from the menu.",
                parse_mode="Markdown",
            )
            return

        now = asyncio.get_event_loop().time()
        p.last_action_time = now
        p.afk_warned = False  # sie haben gerade agiert → Warnung zurücksetzen

        log_lines: List[str] = []
        pre_stage = table.stage

        # ========== PLAYER ACTIONS ==========

        already_all_in = (p.chips <= 0 and action not in ("raise_allin",))

        if already_all_in:
            log_lines.append(
                f"💥 {p.name} is already ALL-IN.\n"
                "No more betting, only pure coping and praying now. 🙏"
            )
        else:
            if action == "call":
                added = table.check_or_call(user.id)
                if added == 0:
                    log_lines.append(f"✅ {p.name} checks.")
                else:
                    log_lines.append(f"✅ {p.name} calls {added} BMT.")

            elif action.startswith("raise_"):
                raw = action.split("_", 1)[1].lower()

                if raw in ("all", "allin", "all-in"):
                    amount = p.chips
                else:
                    try:
                        amount = int(raw)
                    except ValueError:
                        await query.message.reply_text("❌ Invalid raise amount.")
                        return

                added = table.raise_bet(user.id, amount)

                if p.chips <= 0:
                    log_lines.append(f"💥 {p.name} goes *ALL-IN* for {added} BMT!")
                else:
                    log_lines.append(f"💸 {p.name} raises {added} BMT.")

                log_lines.append(f"💼 Stack left: {p.chips} BMT")

            elif action == "raise_allin":
                amount = p.chips
                added = table.raise_bet(user.id, amount)
                log_lines.append(f"💥 {p.name} goes *ALL-IN* for {added} BMT!")
                log_lines.append(f"💼 Stack left: {p.chips} BMT")

            elif action == "fold":
                table.fold(user.id)
                log_lines.append(f"🏳️ {p.name} folds.")

        # First advance after user action (oder "No-Op" bei already_all_in)
        table.advance_stage_if_needed()

        # ========= AUTO-ADVANCE WENN KEIN NÄCHSTER SPIELER / ALLE ALL-IN =========
        active_players = [pl for pl in table.players.values() if not pl.folded]
        all_allin_or_broke = bool(active_players) and all(pl.chips <= 0 for pl in active_players)
        no_next_player = table.current_player_id() is None

        if all_allin_or_broke or no_next_player:
            while True:
                prev_stage = table.stage
                table.advance_stage_if_needed()

                if table.stage in (Stage.FLOP, Stage.TURN, Stage.RIVER) and table.stage != prev_stage:
                    await send_board_images_to_topic(
                        query.message.chat_id,
                        table,
                        context,
                        explain=False,
                        auto_delete=False,  # Board bleibt offen
                    )

                if table.stage == prev_stage or table.stage == Stage.SHOWDOWN:
                    break

            # Fallback: full board, everybody all-in → force showdown
            if (
                table.stage == Stage.RIVER
                and len(table.community_cards) == 5
                and (all_allin_or_broke or no_next_player)
            ):
                table.stage = Stage.SHOWDOWN

        # ========= SEND BOARD IF STREET CHANGED =========
        if table.stage != pre_stage and table.stage in (Stage.FLOP, Stage.TURN, Stage.RIVER):
            await send_board_images_to_topic(
                query.message.chat_id,
                table,
                context,
                explain=False,
                auto_delete=False,  # Board bleibt
            )

        # ========= NÄCHSTEN SPIELER BESTIMMEN (TURN WEITERGEBEN) =========
        if table.stage != Stage.SHOWDOWN and not all_allin_or_broke and not no_next_player:
            table.next_turn()

        # ========= SHOWDOWN ODER NÄCHSTE ACTION =========
        if table.stage == Stage.SHOWDOWN:
            text, markup = await handle_showdown_and_build_text(table, context, query)
        else:
            community = ", ".join(table.community_cards) if table.community_cards else "—"
            next_id = table.current_player_id()

            if next_id is not None and next_id in table.players:
                next_name = table.players[next_id].name
            else:
                next_name = "unknown chaos"

            log_text = "\n".join(log_lines) if log_lines else "…nothing happened?"

            text = (
                f"🃏 BMT Texas Hold'em\n\n"
                f"Stage: {table.stage.name}\n"
                f"Pot: 💰 {table.pot} BMT\n"
                f"Board: {community}\n\n"
                f"{log_text}\n\n"
                f"➡ Next to act: {next_name}"
            )
            markup = build_action_keyboard()

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=markup,
            message_thread_id=table.thread_id,
        )

    except Exception as e:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⚠️ Error in handle_action: {e!r}",
            message_thread_id=table.thread_id,
        )


# ======== HAND EVALUATION HELPERS (Texas Hold'em) ========

CARD_RANKS = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}

RANK_NAMES = {
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "10",
    11: "Jack",
    12: "Queen",
    13: "King",
    14: "Ace",
}


def parse_card(code: str) -> tuple[int, str]:
    code = code.strip().upper()
    if len(code) == 3:
        rank_str = code[:2]
        suit = code[2]
    else:
        rank_str = code[0]
        suit = code[1]
    return CARD_RANKS[rank_str], suit


def detect_straight(ranks: list[int]) -> Optional[int]:
    if len(ranks) < 5:
        return None
    if ranks[0] - ranks[-1] == 4 and len(set(ranks)) == 5:
        return ranks[0]
    if set(ranks) == {14, 5, 4, 3, 2}:
        return 5
    return None


def describe_hand(category: int, ranks_by_count: list[tuple[int, int]],
                  straight_high: Optional[int], flush_high: Optional[int]) -> str:
    if category == 9:
        return "a royal flush"
    if category == 8:
        return f"a straight flush (high card {RANK_NAMES[straight_high]})"
    if category == 7:
        four_rank = ranks_by_count[0][0]
        return f"four of a kind ({RANK_NAMES[four_rank]}s)"
    if category == 6:
        three_rank = ranks_by_count[0][0]
        pair_rank = ranks_by_count[1][0]
        return f"a full house ({RANK_NAMES[three_rank]}s full of {RANK_NAMES[pair_rank]}s)"
    if category == 5:
        return f"a flush (high card {RANK_NAMES[flush_high]})"
    if category == 4:
        return f"a straight to {RANK_NAMES[straight_high]}"
    if category == 3:
        three_rank = ranks_by_count[0][0]
        return f"three of a kind ({RANK_NAMES[three_rank]}s)"
    if category == 2:
        r1 = ranks_by_count[0][0]
        r2 = ranks_by_count[1][0]
        return f"two pair ({RANK_NAMES[r1]}s and {RANK_NAMES[r2]}s)"
    if category == 1:
        pair_rank = ranks_by_count[0][0]
        return f"a pair of {RANK_NAMES[pair_rank]}s"
    high_rank = ranks_by_count[0][0]
    return f"high card {RANK_NAMES[high_rank]}"


def evaluate_5card_hand(cards: list[str]) -> tuple[int, tuple, str]:
    ranks = []
    suits = []
    for c in cards:
        r, s = parse_card(c)
        ranks.append(r)
        suits.append(s)

    ranks_sorted = sorted(ranks, reverse=True)
    unique_ranks_desc = sorted(set(ranks), reverse=True)

    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    is_flush = len(suit_counts) == 1
    flush_high = max(ranks_sorted) if is_flush else None

    straight_high = detect_straight(unique_ranks_desc)
    is_straight = straight_high is not None

    count_by_rank = {}
    for r in ranks:
        count_by_rank[r] = count_by_rank.get(r, 0) + 1

    ranks_by_count = sorted(
        count_by_rank.items(),
        key=lambda x: (x[1], x[0]),
        reverse=True,
    )

    counts = sorted(count_by_rank.values(), reverse=True)

    if is_straight and is_flush and straight_high == 14 and min(ranks_sorted) >= 10:
        category = 9
        key = (9,)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if is_straight and is_flush:
        category = 8
        key = (straight_high,)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if counts[0] == 4:
        four_rank = ranks_by_count[0][0]
        kicker = max(r for r in ranks if r != four_rank)
        category = 7
        key = (four_rank, kicker)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if counts[0] == 3 and counts[1] >= 2:
        three_rank = ranks_by_count[0][0]
        pair_rank = ranks_by_count[1][0]
        category = 6
        key = (three_rank, pair_rank)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if is_flush:
        category = 5
        key = tuple(ranks_sorted)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if is_straight:
        category = 4
        key = (straight_high,)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if counts[0] == 3:
        three_rank = ranks_by_count[0][0]
        kickers = sorted([r for r in ranks if r != three_rank], reverse=True)
        category = 3
        key = (three_rank, *kickers[:2])
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if counts[0] == 2 and counts[1] == 2:
        pair1 = ranks_by_count[0][0]
        pair2 = ranks_by_count[1][0]
        kicker = max(r for r in ranks if r not in (pair1, pair2))
        high_pair, low_pair = max(pair1, pair2), min(pair1, pair2)
        category = 2
        key = (high_pair, low_pair, kicker)
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    if counts[0] == 2:
        pair_rank = ranks_by_count[0][0]
        kickers = sorted([r for r in ranks if r != pair_rank], reverse=True)
        category = 1
        key = (pair_rank, *kickers[:3])
        desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
        return category, key, desc

    category = 0
    key = tuple(ranks_sorted)
    desc = describe_hand(category, ranks_by_count, straight_high, flush_high)
    return category, key, desc


def evaluate_best_hand(board: list[str], hole: list[str]) -> tuple[int, tuple, str, list[str]]:
    all_cards = board + hole
    best_cat = -1
    best_key: tuple = ()
    best_desc = ""
    best_combo: list[str] = []

    for combo in itertools.combinations(all_cards, 5):
        cat, key, desc = evaluate_5card_hand(list(combo))
        if (cat, key) > (best_cat, best_key):
            best_cat = cat
            best_key = key
            best_desc = desc
            best_combo = list(combo)

    return best_cat, best_key, best_desc, best_combo


async def handle_showdown_and_build_text(table: Table, context: ContextTypes.DEFAULT_TYPE, query):
    active = [p for p in table.players.values() if not p.folded]

    if not active:
        text = (
            "🃏 *Showdown!*\n\n"
            "Somehow everyone folded? The pot has vanished into the void.\n"
            "Perfectly decentralized Tears."
        )
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎬 New Hand", callback_data="start_hand")],
                [
                    InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
                    InlineKeyboardButton("📊 Table Stats", callback_data="show_stats"),
                ],
            ]
        )
        return text, markup

    board = table.community_cards
    best_results = []

    for p in active:
        if not p.hole_cards or len(board) < 5:
            best_results.append((-1, (), p, "mysterious non-hand", []))
            continue

        cat, key, desc, best5 = evaluate_best_hand(board, p.hole_cards)
        best_results.append((cat, key, p, desc, best5))

    best_results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    winner_cat, winner_key, winner, winner_desc, winner_best5 = best_results[0]

    winner.chips += table.pot

    storage.record_hand_result(winner.user_id, table.pot, True)
    for p in table.players.values():
        if p.user_id != winner.user_id:
            storage.record_hand_result(p.user_id, 0, False)

    community = ", ".join(table.community_cards) if table.community_cards else "—"
    winning_cards_txt = ", ".join(winner_best5) if winner_best5 else "unknown cardboard"

    text = (
        f"🃏 *Showdown!*\n\n"
        f"Board: {community}\n"
        f"Pot: 💰 {table.pot} BMT\n\n"
        f"🏆 Winner: *{winner.name}* – wins with {winner_desc}.\n"
        f"🂠 Winning 5-card hand: {winning_cards_txt}\n\n"
        "Hit *New Hand* to keep the suffering going."
    )

    next_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎬 New Hand", callback_data="start_hand")],
            [
                InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
                InlineKeyboardButton("📊 Table Stats", callback_data="show_stats"),
            ],
        ]
    )

    # Winner video preferred, fallback to image
    if WINNER_VIDEO_PATH.exists():
        with open(WINNER_VIDEO_PATH, "rb") as v:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=InputFile(v),
                caption=(
                    f"🏆 {winner.name} takes the pot.\n"
                    f"{winner_desc} – {winning_cards_txt}"
                ),
                reply_markup=next_kb,
                message_thread_id=table.thread_id,
            )
    elif WINNER_PATH.exists():
        with open(WINNER_PATH, "rb") as f:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=InputFile(f),
                caption=(
                    f"🏆 {winner.name} wins this one.\n"
                    f"Hand: {winner_desc} – {winning_cards_txt}\n"
                    "Explanation: Best hand at showdown, or everyone else chickened out."
                ),
                reply_markup=next_kb,
                message_thread_id=table.thread_id,
            )

    return text, next_kb

async def send_card_images_to_player(chat_id: int, cards: list[str], context: ContextTypes.DEFAULT_TYPE):
    buf = build_cards_sprite(cards, per_row=2)
    if buf:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(buf, filename="hand.png"),
            caption="🃏 Your hand – don't show this to the maxis.",
        )

    if BACK_PATH.exists():
        with open(BACK_PATH, "rb") as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(f),
                caption="🂠 BMT card back – for everyone else.",
            )


async def send_board_images_to_topic(
    chat_id: int,
    table: Table,
    context: ContextTypes.DEFAULT_TYPE,
    explain: bool = False,
    auto_delete: bool = True,
):
    stage_name = table.stage.name
    caption = (
        f"🂷 Community cards – *{stage_name}*\n"
        "Explanation: These are the shared cards everybody uses."
        if explain
        else f"🂷 Community cards – *{stage_name}*"
    )

    buf = build_cards_sprite(table.community_cards, per_row=2)
    if not buf:
        return

    msg = await context.bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(buf, filename="board.png"),
        caption=caption,
        parse_mode="Markdown",
        message_thread_id=table.thread_id,
    )

    # Nur löschen, wenn ausdrücklich gewünscht (z.B. bei "Show Board")
    if auto_delete and context.application:
        context.application.create_task(
            auto_delete_message(context, chat_id, msg.message_id, delay=25)
        )

    return msg


async def show_match_finished(query, table: Table, context: ContextTypes.DEFAULT_TYPE):
    rows: List[str] = []
    for p in table.players.values():
        diff = p.chips - table.starting_chips
        emo = "📈" if diff > 0 else "📉" if diff < 0 else "➖"
        rows.append(f"{emo} {p.name}: {p.chips} BMT ({diff:+})")

    result_text = "🎉 *Match complete!*\n\nFinal results:\n" + "\n".join(rows)

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅ Back to Menu", callback_data="back_main")],
            [InlineKeyboardButton("🎛 Poker Session Setup", callback_data="setup_menu")],
        ]
    )

    await query.message.reply_text(
        result_text,
        parse_mode="Markdown",
        reply_markup=kb,
    )


# ============== AVATAR + CUSTOM RAISE HANDLER ==============

async def avatar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not update.message.photo:
        return

    await update.message.reply_text(
        "✅ Avatar saved.\n"
        "I will use this face when I mock your decisions at the table."
    )


async def handle_custom_raise_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles numeric text input after clicking 'Custom Raise'."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    if not context.user_data.get("awaiting_custom_raise"):
        return

    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Please enter a number, e.g. 150000.")
        return

    amount = int(text)
    context.user_data["awaiting_custom_raise"] = False

    table = get_table(update, create=False)
    if not table:
        await update.message.reply_text("No table found.")
        return

    user = update.effective_user
    query_like = type("Q", (), {"from_user": user, "message": update.message})
    await handle_action(query_like, table, f"raise_{amount}", context)


# ============== MAIN ==============

def main():
    storage.init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settable", settable))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("fullreset", fullreset_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS,
            handle_custom_raise_input,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.PRIVATE,
            avatar_handler,
        )
    )

    # AFK-Überwachung alle 5 Sekunden
    app.job_queue.run_repeating(afk_watcher_job, interval=5, first=5)

    print("BMT Poker Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()

