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

    # NIGHT choices (каждый игрок роли выбирает цель)
    mafia_target: Dict[int, int] = field(default_factory=dict)     # mafia_id -> target_id
    doctor_save: Dict[int, int] = field(default_factory=dict)      # doctor_id -> target_id
    detective_check: Dict[int, int] = field(default_factory=dict)  # detective_id -> target_id

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

    pool: List[str] = []
    pool += [ROLE_MAFIA] * mafia_count
    pool += [ROLE_DOCTOR] * doctor_count
    pool += [ROLE_DETECTIVE] * detective_count
    while len(pool) < n:
        pool.append(ROLE_CIVILIAN)

    random.shuffle(pool)
    random.shuffle(ids)
    for uid, role in zip(ids, pool):
        g.players[uid].role = role
        g.players[uid].alive = True

    # сброс выборов
    g.mafia_target.clear()
    g.doctor_save.clear()
    g.detective_check.clear()
    g.day_votes.clear()


def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    jq = context.application.job_queue
    if jq is None:
        return
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


def tally_day_votes(g: Game) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for voter_id, target_id in g.day_votes.items():
        if voter_id not in g.players or not g.players[voter_id].alive:
            continue
        counts[target_id] = counts.get(target_id, 0) + 1
    return counts


# =========================
# JOB WRAPPERS (IMPORTANT)
# =========================
async def job_resolve_night(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_night(context, chat_id, by_timer=True)


async def job_begin_day_vote(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await begin_day_vote(context, chat_id)


async def job_resolve_day(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_day(context, chat_id, by_timer=True)


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

    role_text_map = {
        ROLE_MAFIA: "☠️ *Ты МАФИЯ.* Выбирай жертву этой ночью.",
        ROLE_DOCTOR: "🩺 *Ты ДОКТОР.* Выбирай, кого спасти.",
        ROLE_DETECTIVE: "🕵️ *Ты ДЕТЕКТИВ.* Выбирай, кого проверить.",
        ROLE_CIVILIAN: "🙂 *Ты МИРНЫЙ.* Ночью ты спишь.",
    }

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

    for p in mafias:
        await try_dm(context, p.user_id, f"🤝 *Вы мафия.* Состав мафии: *{mafia_names}*{mafia_tip}")

    # ночные кнопки
    for p in alive_players(g):
        if p.role == ROLE_MAFIA:
            await try_dm(context, p.user_id, "Выбери жертву:", reply_markup=kb_night_targets(chat_id, g, "MAFIAKILL", p.user_id))
        elif p.role == ROLE_DOCTOR:
            await try_dm(context, p.user_id, "Кого спасаем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DOCSAVE", p.user_id))
        elif p.role == ROLE_DETECTIVE:
            await try_dm(context, p.user_id, "Кого проверяем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DETCHECK", p.user_id))

    # таймер ночи
    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            job_resolve_night,
            when=NIGHT_SECONDS,
            name=f"night_{chat_id}",
            data={"chat_id": chat_id},
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

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            job_begin_day_vote,
            when=DAY_DISCUSS_SECONDS,
            name=f"day_discuss_{chat_id}",
            data={"chat_id": chat_id},
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

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            job_resolve_day,
            when=DAY_VOTE_SECONDS,
            name=f"day_vote_{chat_id}",
            data={"chat_id": chat_id},
        )


async def resolve_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int, by_timer: bool = False):
    if chat_id not in GAMES:
        return
    g = get_game(chat_id)
    if g.phase != PHASE_NIGHT:
        return

    cancel_jobs(context, chat_id)

    # majority for mafia
    victim_id: Optional[int] = None
    if g.mafia_target:
        counts: Dict[int, int] = {}
        for _, target in g.mafia_target.items():
            counts[target] = counts.get(target, 0) + 1
        # если ничья среди целей мафии — выберем случайно из топа
        mx = max(counts.values())
        top = [tid for tid, v in counts.items() if v == mx]
        victim_id = random.choice(top)

    saved_id: Optional[int] = None
    if g.doctor_save:
        counts: Dict[int, int] = {}
        for _, target in g.doctor_save.items():
            counts[target] = counts.get(target, 0) + 1
        mx = max(counts.values())
        top = [tid for tid, v in counts.items() if v == mx]
        saved_id = random.choice(top)

    # детектив — ответы в личку
    if g.detective_check:
        for det_id, target_id in g.detective_check.items():
            target_role = g.players.get(target_id).role if target_id in g.players else "unknown"
            is_mafia = "ДА" if target_role == ROLE_MAFIA else "НЕТ"
            await try_dm(context, det_id, f"🕵️ Проверка: *{g.players[target_id].name}* — мафия? *{is_mafia}*")

    night_note = "⏰ Ночь закончилась по таймеру." if by_timer else "✅ Ночь закончилась."
    await context.bot.send_message(chat_id=chat_id, text=night_note)

    if victim_id is None:
        await context.bot.send_message(chat_id=chat_id, text="🌫️ Ночь прошла тихо... Никого не выбрали.")
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

    counts = tally_day_votes(g)
    alive_count = len([p for p in g.players.values() if p.alive])

    vote_note = "⏰ Голосование завершилось по таймеру." if by_timer else "✅ Голосование завершено."
    await context.bot.send_message(chat_id=chat_id, text=vote_note)

    if not counts:
        await context.bot.send_message(chat_id=chat_id, text="Пустой суд. Никого не казнили.")
    else:
        sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        top_target, top_votes = sorted_items[0]
        tied = [tid for tid, v in sorted_items if v == top_votes]

        if len(tied) > 1:
            lines = ["⚖️ Ничья. Сегодня никто не казнён.", "", "📊 Итоги голосования:"]
            for tid, v in sorted_items:
                label = "Skip" if tid == 0 else g.players.get(tid).name if tid in g.players else str(tid)
                lines.append(f"• {label}: {v}")
            await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        else:
            if top_target == 0:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏭️ Большинство пропустило. Сегодня без казни. (Skip: {top_votes}/{alive_count})"
                )
            else:
                if top_target in g.players and g.players[top_target].alive:
                    g.players[top_target].alive = False

                lines = [
                    f"⚖️ Приговор вынесен… Казнили: *{g.players[top_target].name}* ☠️",
                    "",
                    "📊 Итоги голосования:"
                ]
                for tid, v in sorted_items:
                    label = "Skip" if tid == 0 else g.players.get(tid).name if tid in g.players else str(tid)
                    lines.append(f"• {label}: {v}")

                await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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

    for p in alive_players(g):
        if p.role == ROLE_MAFIA:
            await try_dm(context, p.user_id, "Выбери жертву:", reply_markup=kb_night_targets(chat_id, g, "MAFIAKILL", p.user_id))
        elif p.role == ROLE_DOCTOR:
            await try_dm(context, p.user_id, "Кого спасаем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DOCSAVE", p.user_id))
        elif p.role == ROLE_DETECTIVE:
            await try_dm(context, p.user_id, "Кого проверяем этой ночью?", reply_markup=kb_night_targets(chat_id, g, "DETCHECK", p.user_id))

    jq = context.application.job_queue
    if jq is not None:
        jq.run_once(
            job_resolve_night,
            when=NIGHT_SECONDS,
            name=f"night_{chat_id}",
            data={"chat_id": chat_id},
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

            # записываем / перезаписываем голос (последний учитывается)
            g.day_votes[uid] = target_id
            await q.answer("Голос принят ✅", show_alert=True)

            # авто-окончание: если все живые проголосовали
            alive_ids = [p.user_id for p in alive_players(g)]
            if alive_ids and all(pid in g.day_votes for pid in alive_ids):
                await context.bot.send_message(chat_id=chat_id, text="✅ Все живые проголосовали. Завершаю голосование.")
                await resolve_day(context, chat_id, by_timer=False)

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

        # раннее завершение ночи, если все роли выбрали
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
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    app.add_handler(CommandHandler("mafia", cmd_mafia))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_text))

    app.add_handler(CallbackQueryHandler(on_button))

    logger.info("GAP Mafia bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
