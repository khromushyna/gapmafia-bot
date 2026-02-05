import os
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG / ENV
# =========================
TOKEN = os.getenv("TOKEN")  # Render -> Environment -> TOKEN
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")  # optional: GAPmafia_bot

MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", "5"))

# таймеры (сек)
NIGHT_SECONDS = int(os.getenv("NIGHT_SECONDS", "60"))
DAY_DISCUSS_SECONDS = int(os.getenv("DAY_DISCUSS_SECONDS", "60"))
DAY_VOTE_SECONDS = int(os.getenv("DAY_VOTE_SECONDS", "60"))

if not TOKEN:
    raise RuntimeError("TOKEN environment variable is not set (Render -> Environment -> TOKEN)")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gapmafia")

# =========================
# GAME MODEL
# =========================
ROLE_MAFIA = "mafia"
ROLE_DOCTOR = "doctor"
ROLE_DETECTIVE = "detective"
ROLE_CIVILIAN = "civilian"

PHASE_LOBBY = "lobby"
PHASE_NIGHT = "night"
PHASE_DAY_DISCUSS = "day_discuss"
PHASE_DAY_VOTE = "day_vote"
PHASE_ENDED = "ended"

@dataclass
class Player:
    user_id: int
    name: str
    role: str = ROLE_CIVILIAN
    alive: bool = True
    opened_dm: bool = False

@dataclass
class Game:
    chat_id: int
    host_id: int
    phase: str = PHASE_LOBBY
    players: Dict[int, Player] = field(default_factory=dict)

    panel_msg_id: Optional[int] = None  # message id панели лобби

    # NIGHT choices
    mafia_target: Dict[int, int] = field(default_factory=dict)
    doctor_save: Dict[int, int] = field(default_factory=dict)
    detective_check: Dict[int, int] = field(default_factory=dict)

    # DAY vote
    day_votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id (0 = skip)

GAMES: Dict[int, Game] = {}  # chat_id -> Game


# =========================
# HELPERS
# =========================
def get_game(chat_id: int) -> Game:
    if chat_id not in GAMES:
        raise RuntimeError("Game not found. Create one with /newgame")
    return GAMES[chat_id]

def deep_link_open_dm(chat_id: int) -> str:
    if not BOT_USERNAME:
        return ""
    return f"https://t.me/{BOT_USERNAME}?start=from_group_{chat_id}"

def alive_players(g: Game) -> List[Player]:
    return [p for p in g.players.values() if p.alive]

def living_ids(g: Game) -> List[int]:
    return [p.user_id for p in alive_players(g)]

def mafia_players(g: Game) -> List[Player]:
    return [p for p in alive_players(g) if p.role == ROLE_MAFIA]

def count_roles(g: Game) -> Tuple[int, int, int, int]:
    mafia = sum(1 for p in g.players.values() if p.alive and p.role == ROLE_MAFIA)
    doc = sum(1 for p in g.players.values() if p.alive and p.role == ROLE_DOCTOR)
    det = sum(1 for p in g.players.values() if p.alive and p.role == ROLE_DETECTIVE)
    civ = sum(1 for p in g.players.values() if p.alive and p.role == ROLE_CIVILIAN)
    return mafia, doc, det, civ

def game_over_text(g: Game) -> Optional[str]:
    mafia, _, _, _ = count_roles(g)
    non_mafia = sum(1 for p in g.players.values() if p.alive and p.role != ROLE_MAFIA)

    if mafia <= 0:
        return "✅ *Победа мирных!* Мафия уничтожена."
    if mafia >= non_mafia:
        return "☠️ *Победа мафии!* Город под контролем."
    return None

def assign_roles(g: Game):
    ids = list(g.players.keys())
    n = len(ids)

    mafia_count = max(1, n // 5)
    doctor_count = 1 if n >= 5 else 0
    detective_count = 1 if n >= 6 else 0

    pool = []
    pool += [ROLE_MAFIA] * mafia_count
    pool += [ROLE_DOCTOR] * doctor_count
    pool += [ROLE_DETECTIVE] * detective_count
    while len(pool) < n:
        pool.append(ROLE_CIVILIAN)

    random.shuffle(pool)
    random.shuffle(ids)
    for uid, role in zip(ids, pool):
        g.players[uid].role = role

def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    jq = context.application.job_queue
    for name in (f"night_{chat_id}", f"day_discuss_{chat_id}", f"day_vote_{chat_id}"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

async def try_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup=None) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except Exception:
        return False


# =========================
# KEYBOARDS
# =========================
def kb_lobby(chat_id: int) -> InlineKeyboardMarkup:
    open_dm_url = deep_link_open_dm(chat_id)
    rows = []
    if open_dm_url:
        rows.append([InlineKeyboardButton("👉 Открыть личку (обязательно для роли/ночи)", url=open_dm_url)])

    rows.append([
        InlineKeyboardButton("✅ JOIN", callback_data=f"JOIN|{chat_id}"),
        InlineKeyboardButton("✅ JOIN + 💬 Личка", callback_data=f"JOIN_DM|{chat_id}"),
    ])
    rows.append([
        InlineKeyboardButton("↩️ LEAVE", callback_data=f"LEAVE|{chat_id}"),
        InlineKeyboardButton("👥 Players", callback_data=f"PLAYERS|{chat_id}"),
    ])
    rows.append([
        InlineKeyboardButton("📌 Status", callback_data=f"STATUS|{chat_id}"),
        InlineKeyboardButton("🎬 Start Game", callback_data=f"START|{chat_id}"),
    ])
    rows.append([InlineKeyboardButton("🛑 End Game", callback_data=f"END|{chat_id}")])
    return InlineKeyboardMarkup(rows)

def kb_day_vote(chat_id: int, g: Game) -> InlineKeyboardMarkup:
    rows = []
    for p in alive_players(g):
        rows.append([InlineKeyboardButton(f"🗳️ Голос за: {p.name}", callback_data=f"VOTE|{chat_id}|{p.user_id}")])
    rows.append([InlineKeyboardButton("⏭️ Skip", callback_data=f"VOTE|{chat_id}|0")])
    rows.append([
        InlineKeyboardButton("✅ Завершить голосование (ведущий)", callback_data=f"ENDVOTE|{chat_id}"),
        InlineKeyboardButton("📌 Status", callback_data=f"STATUS|{chat_id}"),
    ])
    return InlineKeyboardMarkup(rows)

def kb_night_targets(chat_id: int, g: Game, action: str, actor_id: int) -> InlineKeyboardMarkup:
    rows = []
    for p in alive_players(g):
        if p.user_id == actor_id:
            continue
        rows.append([InlineKeyboardButton(p.name, callback_data=f"{action}|{chat_id}|{p.user_id}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"NIGHTCANCEL|{chat_id}")])
    return InlineKeyboardMarkup(rows)


# =========================
# TEXT
# =========================
def lobby_text(chat_id: int, g: Game) -> str:
    names = [p.name for p in g.players.values()]
    names_str = "\n".join(f"• {n}" for n in names) if names else "—"
    return (
        "🎩 *GAP Мафия — Лобби*\n\n"
        "✅ Нажимай *JOIN* прямо тут, в группе.\n"
        "⚠️ Чтобы получать *роль* и *ночные действия* — открой личку и нажми *Start*.\n\n"
        "*Игроки:*\n"
        f"{names_str}\n\n"
        f"Минимум игроков: *{MIN_PLAYERS}*"
    )

def status_text(g: Game) -> str:
    alive = alive_players(g)
    dead = [p for p in g.players.values() if not p.alive]
    alive_str = "\n".join(f"✅ {p.name}" for p in alive) if alive else "—"
    dead_str = "\n".join(f"☠️ {p.name}" for p in dead) if dead else "—"

    mafia, doc, det, civ = count_roles(g)
    return (
        f"📌 *Статус игры*\n"
        f"Фаза: *{g.phase}*\n\n"
        f"*Живые:*\n{alive_str}\n\n"
        f"*Мёртвые:*\n{dead_str}\n\n"
        f"(живых ролей: мафия {mafia}, доктор {doc}, детектив {det}, мирные {civ})"
    )

async def update_lobby_panel(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    if g.phase != PHASE_LOBBY or not g.panel_msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=g.panel_msg_id,
            text=lobby_text(chat_id, g),
            reply_markup=kb_lobby(chat_id),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat and chat.type == ChatType.PRIVATE:
        payload = context.args[0] if context.args else ""
        if payload.startswith("from_group_"):
            try:
                group_chat_id = int(payload.replace("from_group_", "").strip())
                if group_chat_id in GAMES and user.id in GAMES[group_chat_id].players:
                    GAMES[group_chat_id].players[user.id].opened_dm = True
            except Exception:
                pass

        await update.message.reply_text(
            "👋 Привет! Теперь я могу писать тебе *роль* и *ночные действия*.\n\n"
            "Если ты в группе — возвращайся туда и жми JOIN/Start Game.\n\n"
            "💬 *Мафия-чат:* мафия может писать мне в личку командой:\n"
            "`/mafia текст`\n"
            "— я разошлю всем живым мафиям.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("Открой личку с ботом и нажми /start (это нужно Telegram’у).")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды в группе:\n"
        "/newgame — создать лобби\n"
        "/status — статус\n"
        "/endgame — завершить\n\n"
        "Мафия-чат в личке:\n"
        "/mafia текст\n"
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Создавать игру нужно в группе.")
        return

    g = Game(chat_id=chat.id, host_id=user.id)
    GAMES[chat.id] = g
    cancel_jobs(context, chat.id)

    msg = await context.bot.send_message(
        chat_id=chat.id,
        text=lobby_text(chat.id, g),
        reply_markup=kb_lobby(chat.id),
        parse_mode=ParseMode.MARKDOWN,
    )
    g.panel_msg_id = msg.message_id

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.id not in GAMES:
        await update.message.reply_text("Игры нет. Создай /newgame")
        return
    g = get_game(chat.id)
    await update.message.reply_text(status_text(g), parse_mode=ParseMode.MARKDOWN)

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.id not in GAMES:
        await update.message.reply_text("Игры нет.")
        return
    cancel_jobs(context, chat.id)
    del GAMES[chat.id]
    await update.message.reply_text("🛑 Игра завершена.")

async def cmd_mafia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мафия-чат: /mafia message — разошлём всем живым мафиям (в личку)."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != ChatType.PRIVATE:
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Напиши так: `/mafia текст`", parse_mode=ParseMode.MARKDOWN)
        return

    # ищем игру, где этот user является мафией
    for g in GAMES.values():
        if user.id in g.players and g.players[user.id].alive and g.players[user.id].role == ROLE_MAFIA:
            mafias = mafia_players(g)
            for p in mafias:
                if p.user_id == user.id:
                    continue
                await try_dm(context, p.user_id, f"💬 *Мафия-чат:* {g.players[user.id].name}: {text}")
            await update.message.reply_text("✅ Отправлено мафии.")
            return

    await update.message.reply_text("Ты не мафия (или нет активной игры).")


# =========================
# GAME FLOW + TIMERS
# =========================
async def start_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    if g.phase != PHASE_LOBBY:
        return

    if len(g.players) < MIN_PLAYERS:
        await context.bot.send_message(chat_id=chat_id, text=f"Нужно минимум {MIN_PLAYERS} игроков.")
        return

    assign_roles(g)
    g.phase = PHASE_NIGHT

    cancel_jobs(context, chat_id)

    # красивое сообщение
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🌃 *Город засыпает...*\n"
            "Свет гаснет, двери запираются.\n\n"
            f"⏳ *Ночь длится {NIGHT_SECONDS} сек.*\n"
            "Роли и действия — в личке."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # каждому игроку: роль
    role_text_map = {
        ROLE_MAFIA: "☠️ *Ты МАФИЯ.* Выбирай жертву этой ночью.",
        ROLE_DOCTOR: "🩺 *Ты ДОКТОР.* Выбирай, кого спасти.",
        ROLE_DETECTIVE: "🕵️ *Ты ДЕТЕКТИВ.* Выбирай, кого проверить.",
        ROLE_CIVILIAN: "🙂 *Ты МИРНЫЙ.* Ночью ты спишь.",
    }

    # отправим мафии список мафии (“мафия чат”)
    mafias = mafia_players(g)
    mafia_names = ", ".join(p.name for p in mafias) if mafias else ""
    mafia_tip = (
        f"\n\n💬 *Мафия-чат:* пиши в личку боту:\n`/mafia сообщение`\n"
        "— я разошлю всем живым мафиям."
    )

    for p in g.players.values():
        ok = await try_dm(
            context,
            p.user_id,
            f"{role_text_map[p.role]}\n\n(Если бот не пишет — открой личку и нажми /start.)",
        )
        p.opened_dm = ok or p.opened_dm

    # мафии — дополнительно покажем “кто мафия”
    for p in mafias:
        await try_dm(
            context,
            p.user_id,
            f"🤝 *Вы мафия.* Состав мафии: *{mafia_names}*{mafia_tip}",
        )

    # ночные кнопки
    for p in alive_players(g):
        if p.role == ROLE_MAFIA:
            await try_dm(context, p.user_id, "Выбери жертву:", reply_markup=kb_night_targets(chat_id, g, "MAFIAKILL", p.user_id))
        elif p.role == ROLE_DOCTOR:
            await try_dm(context, p.user_id, "Кого спасаем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DOCSAVE", p.user_id))
        elif p.role == ROLE_DETECTIVE:
            await try_dm(context, p.user_id, "Кого проверяем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DETCHECK", p.user_id))

    # таймер ночи: авто-резолв
    context.application.job_queue.run_once(
        lambda ctx: resolve_night(ctx, chat_id, by_timer=True),
        when=NIGHT_SECONDS,
        name=f"night_{chat_id}",
    )

async def begin_day_discuss(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    g.phase = PHASE_DAY_DISCUSS
    g.day_votes.clear()
    cancel_jobs(context, chat_id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🌅 *Город просыпается...*\n"
            "Слышны шаги, кто-то шепчет на улицах.\n\n"
            f"🗣️ *Обсуждение {DAY_DISCUSS_SECONDS} сек.*\n"
            "Потом откроется голосование."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # таймер обсуждения -> открыть голосование
    context.application.job_queue.run_once(
        lambda ctx: begin_day_vote(ctx, chat_id),
        when=DAY_DISCUSS_SECONDS,
        name=f"day_discuss_{chat_id}",
    )

async def begin_day_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    g.phase = PHASE_DAY_VOTE
    g.day_votes.clear()
    cancel_jobs(context, chat_id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⚖️ *Время суда.*\n"
            "Каждый выбирает: кого казнить сегодня?\n\n"
            f"⏳ *Голосование {DAY_VOTE_SECONDS} сек.*"
        ),
        reply_markup=kb_day_vote(chat_id, g),
        parse_mode=ParseMode.MARKDOWN,
    )

    # таймер голосования -> авто-итог
    context.application.job_queue.run_once(
        lambda ctx: resolve_day(ctx, chat_id, by_timer=True),
        when=DAY_VOTE_SECONDS,
        name=f"day_vote_{chat_id}",
    )

async def resolve_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int, by_timer: bool = False):
    if chat_id not in GAMES:
        return
    g = get_game(chat_id)
    if g.phase != PHASE_NIGHT:
        return

    cancel_jobs(context, chat_id)

    victim_id: Optional[int] = None
    if g.mafia_target:
        counts: Dict[int, int] = {}
        for _, target in g.mafia_target.items():
            counts[target] = counts.get(target, 0) + 1
        victim_id = max(counts.items(), key=lambda x: x[1])[0]

    saved_id: Optional[int] = None
    if g.doctor_save:
        counts: Dict[int, int] = {}
        for _, target in g.doctor_save.items():
            counts[target] = counts.get(target, 0) + 1
        saved_id = max(counts.items(), key=lambda x: x[1])[0]

    # детектив — ответы в личку
    if g.detective_check:
        for det_id, target_id in g.detective_check.items():
            target_role = g.players.get(target_id).role if target_id in g.players else "unknown"
            is_mafia = "ДА" if target_role == ROLE_MAFIA else "НЕТ"
            await try_dm(context, det_id, f"🕵️ Проверка: *{g.players[target_id].name}* — мафия? *{is_mafia}*")

    # итог ночи
    night_note = "⏰ Ночь закончилась по таймеру." if by_timer else "✅ Ночь закончилась."
    await context.bot.send_message(chat_id=chat_id, text=night_note)

    if victim_id is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🌫️ Ночь прошла тихо... Никто не был выбран.",
        )
    else:
        if saved_id == victim_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🩺 Доктор успел вовремя. Этой ночью *никто не умер*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            if victim_id in g.players:
                g.players[victim_id].alive = False
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 Утром нашли тело… Это был(а) *{g.players[victim_id].name}* ☠️",
                    parse_mode=ParseMode.MARKDOWN,
                )

    g.mafia_target.clear()
    g.doctor_save.clear()
    g.detective_check.clear()

    end = game_over_text(g)
    if end:
        g.phase = PHASE_ENDED
        await context.bot.send_message(chat_id=chat_id, text=end, parse_mode=ParseMode.MARKDOWN)
        return

    await begin_day_discuss(context, chat_id)

async def resolve_day(context: ContextTypes.DEFAULT_TYPE, chat_id: int, by_timer: bool = False):
    if chat_id not in GAMES:
        return
    g = get_game(chat_id)
    if g.phase != PHASE_DAY_VOTE:
        return

    cancel_jobs(context, chat_id)

    counts: Dict[int, int] = {}
    for voter_id, target_id in g.day_votes.items():
        if voter_id not in g.players or not g.players[voter_id].alive:
            continue
        counts[target_id] = counts.get(target_id, 0) + 1

    vote_note = "⏰ Голосование завершилось по таймеру." if by_timer else "✅ Голосование завершено."
    await context.bot.send_message(chat_id=chat_id, text=vote_note)

    if not counts:
        await context.bot.send_message(chat_id=chat_id, text="Пустой суд. Никого не казнили.")
    else:
        target_id, votes = max(counts.items(), key=lambda x: x[1])
        if target_id == 0:
            await context.bot.send_message(chat_id=chat_id, text="⏭️ Большинство пропустило. Сегодня без казни.")
        else:
            if target_id in g.players and g.players[target_id].alive:
                g.players[target_id].alive = False
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚖️ Приговор вынесен… Казнили: *{g.players[target_id].name}* ☠️",
                    parse_mode=ParseMode.MARKDOWN,
                )

    end = game_over_text(g)
    if end:
        g.phase = PHASE_ENDED
        await context.bot.send_message(chat_id=chat_id, text=end, parse_mode=ParseMode.MARKDOWN)
        return

    # следующая ночь
    g.phase = PHASE_NIGHT
    g.day_votes.clear()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🌃 Снова темнеет…\n"
            f"⏳ *Ночь {NIGHT_SECONDS} сек.* Действия — в личке."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # разошлём ночные кнопки снова
    for p in alive_players(g):
        if p.role == ROLE_MAFIA:
            await try_dm(context, p.user_id, "Выбери жертву:", reply_markup=kb_night_targets(chat_id, g, "MAFIAKILL", p.user_id))
        elif p.role == ROLE_DOCTOR:
            await try_dm(context, p.user_id, "Кого спасаем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DOCSAVE", p.user_id))
        elif p.role == ROLE_DETECTIVE:
            await try_dm(context, p.user_id, "Кого проверяем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DETCHECK", p.user_id))

    # таймер ночи
    context.application.job_queue.run_once(
        lambda ctx: resolve_night(ctx, chat_id, by_timer=True),
        when=NIGHT_SECONDS,
        name=f"night_{chat_id}",
    )


# =========================
# CALLBACKS
# =========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split("|")
    if not parts:
        return

    action = parts[0]

    # GROUP / LOBBY / DAY VOTE
    if action in ("JOIN", "JOIN_DM", "LEAVE", "PLAYERS", "STATUS", "START", "END", "VOTE", "ENDVOTE"):
        if len(parts) < 2:
            return
        chat_id = int(parts[1])
        if chat_id not in GAMES:
            await q.answer("Игры нет. Нужен /newgame", show_alert=True)
            return
        g = get_game(chat_id)

        user = q.from_user
        uid = user.id

        if action == "JOIN":
            if g.phase != PHASE_LOBBY:
                await q.answer("JOIN доступен только в лобби.", show_alert=True)
                return
            if uid in g.players:
                await q.answer("Ты уже в игре ✅", show_alert=True)
                return
            g.players[uid] = Player(user_id=uid, name=user.full_name)
            await update_lobby_panel(context, chat_id)
            await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.full_name} присоединился(лась) к игре.")
            return

        if action == "JOIN_DM":
            if g.phase != PHASE_LOBBY:
                await q.answer("JOIN доступен только в лобби.", show_alert=True)
                return
            if uid not in g.players:
                g.players[uid] = Player(user_id=uid, name=user.full_name)

            ok = await try_dm(
                context,
                uid,
                "✅ Ты в игре!\n\nНажми *Start* здесь в личке один раз — и я смогу присылать роль и ночные действия.",
            )
            g.players[uid].opened_dm = ok or g.players[uid].opened_dm

            await update_lobby_panel(context, chat_id)

            if ok:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.full_name} присоединился(лась) и открыл(а) личку.")
            else:
                link = deep_link_open_dm(chat_id)
                if link:
                    await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.full_name} в игре. Открой личку и нажми Start: {link}")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.full_name} в игре. Открой личку с ботом и нажми /start.")
            return

        if action == "LEAVE":
            if g.phase != PHASE_LOBBY:
                await q.answer("LEAVE доступен только в лобби.", show_alert=True)
                return
            if uid not in g.players:
                await q.answer("Тебя нет в игре.", show_alert=True)
                return
            g.players.pop(uid, None)
            await update_lobby_panel(context, chat_id)
            await context.bot.send_message(chat_id=chat_id, text=f"↩️ {user.full_name} вышел(ла) из игры.")
            return

        if action == "PLAYERS":
            names = [p.name for p in g.players.values()]
            txt = "👥 Игроки:\n" + ("\n".join(f"• {n}" for n in names) if names else "—")
            await q.message.reply_text(txt)
            return

        if action == "STATUS":
            await q.message.reply_text(status_text(g), parse_mode=ParseMode.MARKDOWN)
            return

        if action == "START":
            if g.phase != PHASE_LOBBY:
                await q.answer("Игра уже началась.", show_alert=True)
                return
            if uid != g.host_id:
                await q.answer("Стартовать может только создатель лобби.", show_alert=True)
                return
            await start_game(context, chat_id)
            return

        if action == "END":
            if uid != g.host_id:
                await q.answer("Завершить может только создатель лобби.", show_alert=True)
                return
            cancel_jobs(context, chat_id)
            del GAMES[chat_id]
            await context.bot.send_message(chat_id=chat_id, text="🛑 Игра завершена.")
            return

        if action == "VOTE":
            if len(parts) < 3:
                return
            if g.phase != PHASE_DAY_VOTE:
                await q.answer("Сейчас не время голосования.", show_alert=True)
                return
            target_id = int(parts[2])
            if uid not in g.players or not g.players[uid].alive:
                await q.answer("Ты не живой игрок.", show_alert=True)
                return
            if target_id != 0 and (target_id not in g.players or not g.players[target_id].alive):
                await q.answer("Цель недоступна.", show_alert=True)
                return

            g.day_votes[uid] = target_id
            await q.answer("Голос принят ✅", show_alert=True)
            return

        if action == "ENDVOTE":
            if g.phase != PHASE_DAY_VOTE:
                await q.answer("Сейчас не время голосования.", show_alert=True)
                return
            if uid != g.host_id:
                await q.answer("Завершить голосование может ведущий.", show_alert=True)
                return
            await resolve_day(context, chat_id, by_timer=False)
            return

    # NIGHT actions from DM
    if action in ("MAFIAKILL", "DOCSAVE", "DETCHECK", "NIGHTCANCEL"):
        if len(parts) < 2:
            return
        chat_id = int(parts[1])
        if chat_id not in GAMES:
            await q.answer("Игра не найдена.", show_alert=True)
            return
        g = get_game(chat_id)

        uid = q.from_user.id

        if g.phase != PHASE_NIGHT:
            await q.answer("Сейчас не ночь.", show_alert=True)
            return
        if uid not in g.players or not g.players[uid].alive:
            await q.answer("Ты не живой игрок.", show_alert=True)
            return

        if action == "NIGHTCANCEL":
            await q.edit_message_text("Ок, отменено.")
            return

        if len(parts) < 3:
            return
        target_id = int(parts[2])
        if target_id not in g.players or not g.players[target_id].alive:
            await q.answer("Цель недоступна.", show_alert=True)
            return

        role = g.players[uid].role

        if action == "MAFIAKILL":
            if role != ROLE_MAFIA:
                await q.answer("Ты не мафия.", show_alert=True)
                return
            g.mafia_target[uid] = target_id
            await q.edit_message_text(f"☠️ Цель выбрана: *{g.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)

        elif action == "DOCSAVE":
            if role != ROLE_DOCTOR:
                await q.answer("Ты не доктор.", show_alert=True)
                return
            g.doctor_save[uid] = target_id
            await q.edit_message_text(f"🩺 Спасаем: *{g.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)

        elif action == "DETCHECK":
            if role != ROLE_DETECTIVE:
                await q.answer("Ты не детектив.", show_alert=True)
                return
            g.detective_check[uid] = target_id
            await q.edit_message_text(f"🕵️ Проверяем: *{g.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)

        # если все нужные роли сделали выбор — можно завершить ночь раньше
        mafia_ids = [p.user_id for p in alive_players(g) if p.role == ROLE_MAFIA]
        doc_ids = [p.user_id for p in alive_players(g) if p.role == ROLE_DOCTOR]
        det_ids = [p.user_id for p in alive_players(g) if p.role == ROLE_DETECTIVE]

        mafia_done = all(mid in g.mafia_target for mid in mafia_ids) if mafia_ids else True
        doc_done = all(did in g.doctor_save for did in doc_ids) if doc_ids else True
        det_done = all(tid in g.detective_check for tid in det_ids) if det_ids else True

        if mafia_done and doc_done and det_done:
            await resolve_night(context, chat_id, by_timer=False)

        return


# =========================
# OPTIONAL: “обычный текст” от мафии (в личке) тоже работает как чат
# =========================
async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    txt = (update.message.text or "").strip()
    if not txt:
        return

    # если мафия просто написала сообщение (без /mafia), мы тоже разошлём
    for g in GAMES.values():
        if user.id in g.players and g.players[user.id].alive and g.players[user.id].role == ROLE_MAFIA:
            mafias = mafia_players(g)
            for p in mafias:
                if p.user_id == user.id:
                    continue
                await try_dm(context, p.user_id, f"💬 *Мафия-чат:* {g.players[user.id].name}: {txt}")
            await update.message.reply_text("✅ Отправлено мафии. (Можно и /mafia текст)")
            return


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    # мафия-чат
    app.add_handler(CommandHandler("mafia", cmd_mafia))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_text))

    app.add_handler(CallbackQueryHandler(on_button))

    logger.info("GAP Mafia bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
# GAP Mafia — полноценная игра в группе с кнопками:
# - Лобби: кнопки Открыть личку / Join / Leave / Players / Start / Status / End
# - Ночь: мафия/доктор/комиссар действуют КНОПКАМИ в ЛС
# - День: обсуждение -> голосование КНОПКАМИ в группе
# - Таймеры фаз + досрочное завершение при готовности
#
# Требования: python-telegram-bot==20.7
# TOKEN брать ТОЛЬКО из переменных окружения (Render → Environment → TOKEN)

import os
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable is not set")

# ВАЖНО: username бота (без @). Нужно для кнопки "Открыть личку".
BOT_USERNAME = os.getenv("BOT_USERNAME", "GAPmafia_bot")

MIN_PLAYERS = 5
NIGHT_SECONDS = 60
DAY_DISCUSS_SECONDS = 90
VOTE_SECONDS = 60

# Roles
ROLE_MAFIA = "mafia"
ROLE_DOCTOR = "doctor"
ROLE_SHERIFF = "sheriff"   # комиссар
ROLE_CIVILIAN = "civilian"

# Phases
PHASE_IDLE = "idle"
PHASE_LOBBY = "lobby"
PHASE_NIGHT = "night"
PHASE_DAY = "day"
PHASE_VOTE = "vote"
PHASE_ENDED = "ended"

# Logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# DATA
# =========================
@dataclass
class Player:
    user_id: int
    name: str
    alive: bool = True
    role: Optional[str] = None


@dataclass
class Game:
    chat_id: int
    phase: str = PHASE_IDLE

    players: Dict[int, Player] = field(default_factory=dict)

    # Roles (IDs)
    mafia_ids: Set[int] = field(default_factory=set)
    doctor_id: Optional[int] = None
    sheriff_id: Optional[int] = None

    # Night actions
    mafia_votes: Dict[int, int] = field(default_factory=dict)  # mafia_uid -> target_uid
    doctor_save: Optional[int] = None
    sheriff_check: Optional[int] = None

    # Day vote
    day_votes: Dict[int, int] = field(default_factory=dict)  # voter_uid -> target_uid

    # Timers (job names)
    phase_job_name: Optional[str] = None

    # Panel message id (optional, only for reference)
    panel_msg_id: Optional[int] = None

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_ids(self) -> List[int]:
        return [p.user_id for p in self.players.values() if p.alive]

    def role_alive(self, uid: Optional[int]) -> bool:
        return bool(uid and uid in self.players and self.players[uid].alive)

    def mafia_alive_ids(self) -> List[int]:
        return [uid for uid in self.mafia_ids if uid in self.players and self.players[uid].alive]


GAMES: Dict[int, Game] = {}


def get_game(chat_id: int) -> Game:
    if chat_id not in GAMES:
        GAMES[chat_id] = Game(chat_id=chat_id)
    return GAMES[chat_id]


def is_group(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type in (ChatType.GROUP, ChatType.SUPERGROUP))


def role_ru(role: str) -> str:
    return {
        ROLE_MAFIA: "😈 Мафия",
        ROLE_DOCTOR: "🩺 Доктор",
        ROLE_SHERIFF: "🕵️ Комиссар",
        ROLE_CIVILIAN: "🙂 Мирный",
    }.get(role, role)


def cancel_job(context: ContextTypes.DEFAULT_TYPE, g: Game):
    if not g.phase_job_name:
        return
    for j in context.job_queue.get_jobs_by_name(g.phase_job_name):
        j.schedule_removal()
    g.phase_job_name = None


async def safe_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup=None) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return True
    except Exception:
        return False


def deep_link(chat_id: int) -> str:
    # Кнопка открывает личку и отправляет /start game_<chat_id>
    return f"https://t.me/{BOT_USERNAME}?start=game_{chat_id}"


def kb_panel(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👉 Открыть личку (обязательно)", url=deep_link(chat_id))],
        [
            InlineKeyboardButton("✅ JOIN", callback_data=f"{chat_id}|JOIN|0"),
            InlineKeyboardButton("↩️ LEAVE", callback_data=f"{chat_id}|LEAVE|0"),
        ],
        [
            InlineKeyboardButton("👥 Players", callback_data=f"{chat_id}|PLAYERS|0"),
            InlineKeyboardButton("📌 Status", callback_data=f"{chat_id}|STATUS|0"),
        ],
        [
            InlineKeyboardButton("🎬 Start Game", callback_data=f"{chat_id}|START|0"),
            InlineKeyboardButton("🛑 End Game", callback_data=f"{chat_id}|END|0"),
        ],
    ])


def kb_targets(chat_id: int, action: str, g: Game, actor_uid: Optional[int] = None, allow_self: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for p in g.alive_players():
        if actor_uid and not allow_self and p.user_id == actor_uid:
            continue
        rows.append([InlineKeyboardButton(p.name, callback_data=f"{chat_id}|{action}|{p.user_id}")])
    if not rows:
        rows = [[InlineKeyboardButton("Нет целей", callback_data=f"{chat_id}|NOOP|0")]]
    return InlineKeyboardMarkup(rows)


def build_roles(n: int) -> List[str]:
    # Баланс: 1 мафия на 4 игрока (мин 1), + доктор + комиссар, остальное мирные.
    mafia_count = max(1, n // 4)
    roles = [ROLE_MAFIA] * mafia_count + [ROLE_DOCTOR, ROLE_SHERIFF]
    roles = roles[:n]
    roles += [ROLE_CIVILIAN] * (n - len(roles))
    random.shuffle(roles)
    return roles


def check_win(g: Game) -> Optional[str]:
    alive = g.alive_players()
    mafia_alive = sum(1 for p in alive if p.role == ROLE_MAFIA)
    town_alive = len(alive) - mafia_alive

    if mafia_alive <= 0:
        return "🎉 *Мирные победили!* Мафия устранена."
    if mafia_alive >= town_alive:
        return "💀 *Мафия победила!* Мафии стало не меньше, чем мирных."
    return None


def mafia_kill_target(g: Game) -> Optional[int]:
    # Мафия голосует. Берём цель с большинством, при ничьей — случайно среди топа.
    votes = [t for uid, t in g.mafia_votes.items() if uid in g.mafia_ids and g.players.get(uid) and g.players[uid].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [t for t, c in counts.items() if c == mx]
    return random.choice(top)


def everyone_voted(g: Game) -> bool:
    alive_ids = set(g.alive_ids())
    voters = set(uid for uid in g.day_votes.keys() if uid in alive_ids)
    return alive_ids.issubset(voters)


def required_night_done(g: Game) -> bool:
    mafia_done = (len(g.mafia_alive_ids()) == 0) or (mafia_kill_target(g) is not None)
    doctor_done = (not g.role_alive(g.doctor_id)) or (g.doctor_save is not None)
    sheriff_done = (not g.role_alive(g.sheriff_id)) or (g.sheriff_check is not None)
    return mafia_done and doctor_done and sheriff_done


# =========================
# FLOW
# =========================
async def start_lobby(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    cancel_job(context, g)

    g.phase = PHASE_LOBBY
    g.players.clear()
    g.mafia_ids.clear()
    g.doctor_id = None
    g.sheriff_id = None

    g.mafia_votes.clear()
    g.doctor_save = None
    g.sheriff_check = None
    g.day_votes.clear()

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎩 *GAP Мафия — лобби создано!*\n\n"
            "1) Нажмите *Открыть личку* (обязательно, чтобы получать роль и ночные действия)\n"
            "2) Нажмите *JOIN*\n"
            "3) Нажмите *Start Game*\n\n"
            f"Минимум игроков: *{MIN_PLAYERS}*"
        ),
        reply_markup=kb_panel(chat_id),
        parse_mode=ParseMode.MARKDOWN
    )
    g.panel_msg_id = msg.message_id


async def start_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    if g.phase != PHASE_LOBBY:
        await context.bot.send_message(chat_id=chat_id, text="Сначала /newgame (или кнопка).")
        return
    if len(g.players) < MIN_PLAYERS:
        await context.bot.send_message(chat_id=chat_id, text=f"Нужно минимум {MIN_PLAYERS} игроков.")
        return

    ids = list(g.players.keys())
    random.shuffle(ids)
    roles = build_roles(len(ids))

    g.mafia_ids.clear()
    g.doctor_id = None
    g.sheriff_id = None

    for uid, r in zip(ids, roles):
        g.players[uid].role = r
        if r == ROLE_MAFIA:
            g.mafia_ids.add(uid)
        elif r == ROLE_DOCTOR:
            g.doctor_id = uid
        elif r == ROLE_SHERIFF:
            g.sheriff_id = uid

    # DM roles (если кто-то не нажал Start — будет fail)
    failed: List[str] = []
    for uid in ids:
        p = g.players[uid]
        ok = await safe_dm(context, uid, f"🎭 Твоя роль: *{role_ru(p.role or '?')}*\n\nНе показывай никому.")
        if not ok:
            failed.append(p.name)

    if failed:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Не смог отправить роль этим игрокам (они не нажали Start у бота в личке):\n"
                + "\n".join(f"• {n}" for n in failed)
                + "\n\nПусть нажмут кнопку *Открыть личку* и вернутся. Затем попробуйте Start Game ещё раз."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        # Оставляем игру в лобби, чтобы не ломать
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ Игра началась! Роли отправлены в личку. Наступает ночь…"
    )
    await start_night(context, chat_id)


async def start_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    cancel_job(context, g)

    g.phase = PHASE_NIGHT
    g.mafia_votes.clear()
    g.doctor_save = None
    g.sheriff_check = None

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 *НОЧЬ* (⏳ {NIGHT_SECONDS} сек). Роли действуют в личке.",
        parse_mode=ParseMode.MARKDOWN
    )

    # Send night action panels in DM
    # Mafia
    for mid in g.mafia_alive_ids():
        await safe_dm(
            context, mid,
            "😈 *Ты мафия.* Выбери жертву этой ночью:",
            reply_markup=kb_targets(chat_id, "NKILL", g, actor_uid=mid, allow_self=False)
        )
    # Doctor
    if g.role_alive(g.doctor_id):
        await safe_dm(
            context, g.doctor_id,
            "🩺 *Ты доктор.* Выбери, кого лечить:",
            reply_markup=kb_targets(chat_id, "NHEAL", g, actor_uid=g.doctor_id, allow_self=True)
        )
    # Sheriff
    if g.role_alive(g.sheriff_id):
        await safe_dm(
            context, g.sheriff_id,
            "🕵️ *Ты комиссар.* Выбери, кого проверить (результат придёт сюда):",
            reply_markup=kb_targets(chat_id, "NCHECK", g, actor_uid=g.sheriff_id, allow_self=False)
        )

    # Schedule night timeout
    g.phase_job_name = f"phase_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(night_timeout, when=NIGHT_SECONDS, name=g.phase_job_name, data={"chat_id": chat_id})


async def night_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_night(context, chat_id, forced=True)


async def resolve_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int, forced: bool):
    g = get_game(chat_id)
    if g.phase != PHASE_NIGHT:
        return
    cancel_job(context, g)

    if forced:
        await context.bot.send_message(chat_id=chat_id, text="⏰ Ночь закончилась (таймер).")

    mafia_target = mafia_kill_target(g)
    saved = g.doctor_save
    checked = g.sheriff_check

    # Sheriff DM result
    if checked is not None and g.role_alive(g.sheriff_id) and checked in g.players:
        is_mafia = (g.players[checked].role == ROLE_MAFIA)
        await safe_dm(
            context, g.sheriff_id,
            f"🕵️ Проверка: *{g.players[checked].name}* — {'😈 МАФИЯ' if is_mafia else '🙂 НЕ мафия'}"
        )

    killed_name = None
    if mafia_target is not None and mafia_target in g.players and g.players[mafia_target].alive:
        if saved == mafia_target:
            killed_name = None
        else:
            g.players[mafia_target].alive = False
            killed_name = g.players[mafia_target].name

    if killed_name:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"☀️ Утро. Ночью убили: *{killed_name}*",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text="☀️ Утро. Ночью никто не погиб.")

    win = check_win(g)
    if win:
        await end_game(context, chat_id, win)
        return

    await start_day(context, chat_id)


async def start_day(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    g = get_game(chat_id)
    cancel_job(context, g)

    g.phase = PHASE_DAY
    g.day_votes.clear()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🗣 *День — обсуждение* (⏳ {DAY_DISCUSS_SECONDS} сек)\n\n"
            "Живые:\n" + "\n".join(f"• {p.name}" for p in g.alive_players())
        ),
        parse_mode=ParseMode.MARKDOWN
    )

    g.phase_job_name = f"phase_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(day_timeout, when=DAY_DISCUSS_SECONDS, name=g.phase_job_name, data={"chat_id": chat_id})


async def day_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await start_vote(context, chat_id, forced=True)


async def start_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int, forced: bool):
    g = get_game(chat_id)
    if g.phase not in (PHASE_DAY, PHASE_VOTE):
        return
    cancel_job(context, g)

    g.phase = PHASE_VOTE
    g.day_votes.clear()

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🗳 *Голосование* (⏳ {VOTE_SECONDS} сек)\nНажми кнопку, кого выгоняем.",
        parse_mode=ParseMode.MARKDOWN
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text="Выбор цели:",
        reply_markup=kb_targets(chat_id, "DVOTE", g, actor_uid=None, allow_self=False),
    )

    g.phase_job_name = f"phase_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(vote_timeout, when=VOTE_SECONDS, name=g.phase_job_name, data={"chat_id": chat_id})


async def vote_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_vote(context, chat_id, forced=True)


def vote_result(g: Game) -> Optional[int]:
    # last vote counts per voter. choose max; tie => random among top
    votes = [t for voter, t in g.day_votes.items() if voter in g.players and g.players[voter].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [uid for uid, c in counts.items() if c == mx]
    top = [uid for uid in top if uid in g.players and g.players[uid].alive]
    return random.choice(top) if top else None


async def resolve_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int, forced: bool):
    g = get_game(chat_id)
    if g.phase != PHASE_VOTE:
        return
    cancel_job(context, g)

    if forced:
        await context.bot.send_message(chat_id=chat_id, text="⏰ Голосование завершено.")

    target = vote_result(g)
    if target is None:
        await context.bot.send_message(chat_id=chat_id, text="🤷 Никого не выбрали. Начинается ночь.")
        await start_night(context, chat_id)
        return

    g.players[target].alive = False
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚪 Выгнали: *{g.players[target].name}*",
        parse_mode=ParseMode.MARKDOWN
    )

    win = check_win(g)
    if win:
        await end_game(context, chat_id, win)
        return

    await start_night(context, chat_id)


async def end_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason_md: str):
    g = get_game(chat_id)
    cancel_job(context, g)
    g.phase = PHASE_ENDED

    lines = ["🎭 *Роли игроков:*"]
    for p in g.players.values():
        icon = "🟢" if p.alive else "⚫"
        lines.append(f"{icon} {p.name} — *{role_ru(p.role or '?')}*")

    await context.bot.send_message(
        chat_id=chat_id,
        text=reason_md + "\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN
    )
    # reset game slot
    del GAMES[chat_id]


# =========================
# COMMANDS (минимум — всё можно кнопками, но команды оставляем)
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link support: /start game_<chat_id>
    if update.effective_chat.type == ChatType.PRIVATE:
        if context.args and context.args[0].startswith("game_"):
            await update.message.reply_text(
                "✅ Личка открыта!\n\n"
                "Вернись в группу и нажми JOIN (или /join). "
                "Роли и ночные действия будут приходить сюда."
            )
        else:
            await update.message.reply_text(
                "🎩 GAP Мафия\n\n"
                "Играй в группе: добавь бота в чат и нажми /newgame."
            )
    else:
        await update.message.reply_text("🎩 GAP Мафия\n\nНажми /newgame чтобы создать лобби.")


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text("Создавать игру нужно в группе.")
        return
    await start_lobby(context, update.effective_chat.id)


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if g.phase == PHASE_IDLE:
        await update.message.reply_text("Нет активной игры. /newgame")
        return
    if not g.players:
        await update.message.reply_text("Пока нет игроков.")
        return
    txt = "👥 Игроки:\n" + "\n".join(f"• {p.name}" for p in g.players.values())
    await update.message.reply_text(txt)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if g.phase == PHASE_IDLE:
        await update.message.reply_text("Нет активной игры. /newgame")
        return
    alive = [p.name for p in g.alive_players()]
    dead = [p.name for p in g.players.values() if not p.alive]
    await update.message.reply_text(
        f"📌 Фаза: {g.phase}\n"
        f"🟢 Живые ({len(alive)}): " + (", ".join(alive) if alive else "—") + "\n"
        f"⚫ Выбыли ({len(dead)}): " + (", ".join(dead) if dead else "—")
    )


async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    chat_id = update.effective_chat.id
    if chat_id in GAMES:
        await end_game(context, chat_id, "🛑 Игра остановлена командой /endgame.")
    else:
        await update.message.reply_text("Нет активной игры.")


# =========================
# CALLBACKS (кнопки)
# =========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split("|")
    if len(parts) != 3:
        await q.answer()
        return

    chat_id = int(parts[0])
    action = parts[1]
    target_uid = int(parts[2])

    g = get_game(chat_id)
    user = q.from_user
    uid = user.id

    # NOOP
    if action == "NOOP":
        await q.answer()
        return

    # ===== Panel buttons in GROUP =====
    if action in ("JOIN", "LEAVE", "PLAYERS", "STATUS", "START", "END"):
        # Only allow these from group chat
        if q.message.chat.type not in ("group", "supergroup"):
            await q.answer("Эта кнопка работает в группе.", show_alert=True)
            return

        if action == "JOIN":
            if g.phase not in (PHASE_LOBBY,):
                await q.answer("Сейчас не лобби.", show_alert=True)
                return
            if uid in g.players:
                await q.answer("Ты уже в игре ✅", show_alert=True)
                return
            g.players[uid] = Player(user_id=uid, name=user.full_name)
            await q.answer("Ты вошёл(ла) ✅", show_alert=True)
            return

        if action == "LEAVE":
            if g.phase not in (PHASE_LOBBY,):
                await q.answer("Выйти можно только в лобби.", show_alert=True)
                return
            if uid not in g.players:
                await q.answer("Тебя нет в лобби.", show_alert=True)
                return
            g.players.pop(uid, None)
            await q.answer("Вышел(ла) ↩️", show_alert=True)
            return

        if action == "PLAYERS":
            if g.phase == PHASE_IDLE:
                await q.answer("Нет игры. /newgame", show_alert=True)
                return
            txt = "👥 Игроки:\n" + ("\n".join(f"• {p.name}" for p in g.players.values()) if g.players else "—")
            await q.answer("Показал список", show_alert=False)
            await context.bot.send_message(chat_id=chat_id, text=txt)
            return

        if action == "STATUS":
            if g.phase == PHASE_IDLE:
                await q.answer("Нет игры. /newgame", show_alert=True)
                return
            alive = [p.name for p in g.alive_players()]
            await q.answer("Ок", show_alert=False)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📌 Фаза: {g.phase}\n🟢 Живые ({len(alive)}): " + (", ".join(alive) if alive else "—")
            )
            return

        if action == "START":
            await q.answer("Запускаю…", show_alert=False)
            await start_game(context, chat_id)
            return

        if action == "END":
            await q.answer("Останавливаю…", show_alert=False)
            if chat_id in GAMES:
                await end_game(context, chat_id, "🛑 Игра остановлена кнопкой.")
            return

    # ===== Night actions in PRIVATE =====
    if action in ("NKILL", "NHEAL", "NCHECK"):
        if q.message.chat.type != "private":
            await q.answer("Ночное действие — в личке.", show_alert=True)
            return

        # validate: must be a player and alive
        actor = g.players.get(uid)
        if not actor or not actor.alive:
            await q.answer("Ты не игрок или уже выбыл(а).", show_alert=True)
            return
        if target_uid not in g.players or not g.players[target_uid].alive:
            await q.answer("Цель недоступна.", show_alert=True)
            return

        if g.phase != PHASE_NIGHT:
            await q.answer("Сейчас не ночь.", show_alert=True)
            return

        if action == "NKILL":
            if actor.role != ROLE_MAFIA:
                await q.answer("Ты не мафия.", show_alert=True)
                return
            if target_uid == uid:
                await q.answer("Нельзя выбрать себя.", show_alert=True)
                return
            g.mafia_votes[uid] = target_uid
            await q.answer("Принято ✅", show_alert=True)
            try:
                await q.edit_message_text(f"😈 Ты проголосовал(а) убить: *{g.players[target_uid].name}*", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        elif action == "NHEAL":
            if uid != g.doctor_id or actor.role != ROLE_DOCTOR:
                await q.answer("Ты не доктор.", show_alert=True)
                return
            g.doctor_save = target_uid
            await q.answer("Принято ✅", show_alert=True)
            try:
                await q.edit_message_text(f"🩺 Ты лечишь: *{g.players[target_uid].name}*", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        elif action == "NCHECK":
            if uid != g.sheriff_id or actor.role != ROLE_SHERIFF:
                await q.answer("Ты не комиссар.", show_alert=True)
                return
            if target_uid == uid:
                await q.answer("Нельзя проверять себя.", show_alert=True)
                return
            g.sheriff_check = target_uid
            await q.answer("Принято ✅", show_alert=True)
            try:
                await q.edit_message_text(f"🕵️ Ты проверяешь: *{g.players[target_uid].name}*", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        # resolve early if ready
        if required_night_done(g):
            await resolve_night(context, chat_id, forced=False)
        return

    # ===== Day voting in GROUP =====
    if action == "DVOTE":
        if q.message.chat.type not in ("group", "supergroup"):
            await q.answer("Голосование — в группе.", show_alert=True)
            return
        if g.phase != PHASE_VOTE:
            await q.answer("Сейчас нет голосования.", show_alert=True)
            return

        voter = g.players.get(uid)
        if not voter or not voter.alive:
            await q.answer("Ты не игрок или уже выбыл(а).", show_alert=True)
            return
        if target_uid == uid:
            await q.answer("Нельзя голосовать за себя.", show_alert=True)
            return
        if target_uid not in g.players or not g.players[target_uid].alive:
            await q.answer("Цель недоступна.", show_alert=True)
            return

        # allow changing vote: overwrite
        g.day_votes[uid] = target_uid
        await q.answer(f"Голос принят ✅ ({g.players[target_uid].name})", show_alert=False)

        # if majority achieved, end early
        alive_count = len(g.alive_ids())
        needed = (alive_count // 2) + 1
        counts: Dict[int, int] = {}
        for v, t in g.day_votes.items():
            if v in g.players and g.players[v].alive and t in g.players and g.players[t].alive:
                counts[t] = counts.get(t, 0) + 1
        for t, c in counts.items():
            if c >= needed:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Большинство ({c}/{alive_count}). Завершаю голосование.")
                await resolve_vote(context, chat_id, forced=False)
                return

        # or if everyone voted
        if everyone_voted(g):
            await resolve_vote(context, chat_id, forced=False)
        return

    await q.answer()


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    # Buttons
    app.add_handler(CallbackQueryHandler(on_button))

    logger.info("GAP Mafia bot running (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
