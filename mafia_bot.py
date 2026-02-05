import os
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# TOKEN (ENV ONLY)
# =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable is not set")

# =========================
# GAME SETTINGS
# =========================
NIGHT_SECONDS = 60
DAY_DISCUSSION_SECONDS = 90
VOTE_SECONDS = 60

MIN_PLAYERS = 5  # рекомендовано 5+ (мафия/доктор/комиссар)

ROLE_MAFIA = "mafia"
ROLE_DOCTOR = "doctor"
ROLE_DETECTIVE = "detective"
ROLE_CITIZEN = "citizen"

PHASE_WAITING = "waiting"
PHASE_NIGHT = "night"
PHASE_DAY_DISCUSS = "day_discuss"
PHASE_VOTE = "vote"
PHASE_ENDED = "ended"


# =========================
# MODELS
# =========================
@dataclass
class Player:
    user_id: int
    name: str
    role: Optional[str] = None
    alive: bool = True


@dataclass
class MafiaGame:
    active: bool = False
    phase: str = PHASE_WAITING
    players: Dict[int, Player] = field(default_factory=dict)

    # night actions
    mafia_target: Optional[int] = None
    doctor_save: Optional[int] = None
    detective_check: Optional[int] = None
    detective_result: Optional[Tuple[int, bool]] = None  # (checked_id, is_mafia)

    # tracking who acted
    mafia_voters: Set[int] = field(default_factory=set)
    doctor_acted: bool = False
    detective_acted: bool = False

    # day vote
    vote_counts: Dict[int, int] = field(default_factory=dict)
    voted_by: Set[int] = field(default_factory=set)

    # jobs
    phase_job_name: Optional[str] = None

    # last message ids (optional)
    last_group_prompt: Optional[int] = None

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def alive_ids(self) -> List[int]:
        return [p.user_id for p in self.players.values() if p.alive]

    def role_ids(self, role: str) -> List[int]:
        return [p.user_id for p in self.players.values() if p.role == role and p.alive]


games: Dict[int, MafiaGame] = {}  # chat_id -> game


# =========================
# HELPERS
# =========================
def get_game(chat_id: int) -> MafiaGame:
    if chat_id not in games:
        games[chat_id] = MafiaGame()
    return games[chat_id]


def cancel_phase_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if game.phase_job_name:
        jobs = context.job_queue.get_jobs_by_name(game.phase_job_name)
        for j in jobs:
            j.schedule_removal()
        game.phase_job_name = None


def pick_roles(game: MafiaGame) -> bool:
    ids = list(game.players.keys())
    if len(ids) < MIN_PLAYERS:
        return False

    random.shuffle(ids)

    # 1 мафия на 4 игрока, минимум 1
    mafia_count = max(1, len(ids) // 4)

    roles = []
    roles += [ROLE_MAFIA] * mafia_count
    roles += [ROLE_DOCTOR]
    roles += [ROLE_DETECTIVE]
    remaining = len(ids) - len(roles)
    roles += [ROLE_CITIZEN] * max(0, remaining)

    random.shuffle(roles)

    for uid, role in zip(ids, roles):
        game.players[uid].role = role

    return True


def build_targets_keyboard(game: MafiaGame, action: str, exclude_self_id: Optional[int] = None) -> InlineKeyboardMarkup:
    # action: "kill", "save", "check", "vote"
    buttons = []
    for p in game.alive_players():
        if exclude_self_id is not None and p.user_id == exclude_self_id:
            continue
        label = p.name
        cb = f"{action}:{p.user_id}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    return InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup(
        [[InlineKeyboardButton("Нет доступных целей", callback_data="noop")]]
    )


def format_alive_list(game: MafiaGame) -> str:
    lines = []
    for p in game.players.values():
        status = "🟢" if p.alive else "🔴"
        lines.append(f"{status} {p.name}")
    return "\n".join(lines) if lines else "—"


def mafia_win_check(game: MafiaGame) -> Optional[str]:
    """Return winner role string if game ended, else None."""
    alive = game.alive_players()
    if not alive:
        return "none"

    mafia_alive = sum(1 for p in alive if p.role == ROLE_MAFIA)
    others_alive = len(alive) - mafia_alive

    if mafia_alive == 0:
        return "citizens"
    if mafia_alive >= others_alive:
        return "mafia"
    return None


async def safe_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup=None):
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception:
        return False


async def announce(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None):
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


def reset_night_state(game: MafiaGame):
    game.mafia_target = None
    game.doctor_save = None
    game.detective_check = None
    game.detective_result = None

    game.mafia_voters.clear()
    game.doctor_acted = False
    game.detective_acted = False


def reset_vote_state(game: MafiaGame):
    game.vote_counts.clear()
    game.voted_by.clear()


async def end_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str):
    game = get_game(chat_id)
    game.active = False
    game.phase = PHASE_ENDED
    cancel_phase_job(context, chat_id)

    # reveal roles
    reveal = []
    for p in game.players.values():
        role = p.role or "?"
        reveal.append(f"{'🟢' if p.alive else '🔴'} {p.name} — *{role}*")
    reveal_text = "\n".join(reveal) if reveal else "—"

    await announce(
        context,
        chat_id,
        f"🛑 *Игра завершена*\nПричина: {reason}\n\n*Роли:*\n{reveal_text}",
    )


# =========================
# PHASE FLOW
# =========================
async def start_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if not game.active:
        return

    cancel_phase_job(context, chat_id)
    reset_night_state(game)
    game.phase = PHASE_NIGHT

    await announce(
        context, chat_id,
        "🌙 *Ночь наступила.*\nМафия выбирает жертву. Доктор — кого лечит. Комиссар — кого проверяет."
    )

    # DM mafia
    mafia_ids = game.role_ids(ROLE_MAFIA)
    for mid in mafia_ids:
        await safe_dm(
            context, mid,
            "😈 *Ты мафия.* Выбери, кого убрать этой ночью:",
            reply_markup=build_targets_keyboard(game, "kill", exclude_self_id=mid)
        )

    # DM doctor
    doctor_ids = game.role_ids(ROLE_DOCTOR)
    for did in doctor_ids:
        await safe_dm(
            context, did,
            "🩺 *Ты доктор.* Выбери, кого лечить этой ночью:",
            reply_markup=build_targets_keyboard(game, "save")
        )

    # DM detective
    det_ids = game.role_ids(ROLE_DETECTIVE)
    for cid in det_ids:
        await safe_dm(
            context, cid,
            "🕵️ *Ты комиссар.* Выбери, кого проверить:",
            reply_markup=build_targets_keyboard(game, "check", exclude_self_id=cid)
        )

    # schedule night end
    job_name = f"phase_{chat_id}"
    game.phase_job_name = job_name
    context.job_queue.run_once(
        night_timeout_job,
        when=NIGHT_SECONDS,
        name=job_name,
        data={"chat_id": chat_id},
    )


async def night_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_night(context, chat_id, timed_out=True)


async def resolve_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int, timed_out: bool):
    game = get_game(chat_id)
    if not game.active or game.phase != PHASE_NIGHT:
        return

    cancel_phase_job(context, chat_id)

    # determine mafia target (simple: if multiple mafia, accept first consistent vote;
    # to keep it simple: last selected target by any mafia overrides)
    target_id = game.mafia_target

    saved_id = game.doctor_save

    killed_player: Optional[Player] = None
    if target_id and target_id in game.players and game.players[target_id].alive:
        if saved_id == target_id:
            # saved
            killed_player = None
        else:
            game.players[target_id].alive = False
            killed_player = game.players[target_id]

    # detective result DM
    if game.detective_check and game.detective_check in game.players:
        checked = game.players[game.detective_check]
        is_mafia = (checked.role == ROLE_MAFIA)
        game.detective_result = (checked.user_id, is_mafia)
        for cid in game.role_ids(ROLE_DETECTIVE):
            await safe_dm(
                context, cid,
                f"🕵️ Результат проверки: *{checked.name}* — {'😈 МАФИЯ' if is_mafia else '🙂 НЕ мафия'}"
            )

    if killed_player:
        await announce(context, chat_id, f"☀️ *Утро.* Ночью был убит: *{killed_player.name}*")
    else:
        msg = "☀️ *Утро.* Ночью никто не умер."
        if timed_out and not target_id:
            msg += "\n(Мафия не успела выбрать.)"
        await announce(context, chat_id, msg)

    winner = mafia_win_check(game)
    if winner:
        if winner == "mafia":
            await end_game(context, chat_id, "Мафия победила (мафии не меньше, чем мирных).")
        elif winner == "citizens":
            await end_game(context, chat_id, "Мирные победили (вся мафия устранена).")
        return

    await start_day_discussion(context, chat_id)


async def start_day_discussion(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if not game.active:
        return

    cancel_phase_job(context, chat_id)
    game.phase = PHASE_DAY_DISCUSS
    reset_vote_state(game)

    await announce(
        context, chat_id,
        f"🗣 *День. Обсуждение ({DAY_DISCUSSION_SECONDS} сек).* \n\n*Живые игроки:*\n{format_alive_list(game)}\n\nЧерез время начнётся голосование."
    )

    job_name = f"phase_{chat_id}"
    game.phase_job_name = job_name
    context.job_queue.run_once(
        day_discuss_timeout_job,
        when=DAY_DISCUSSION_SECONDS,
        name=job_name,
        data={"chat_id": chat_id},
    )


async def day_discuss_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await start_vote(context, chat_id)


async def start_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if not game.active:
        return
    if game.phase not in (PHASE_DAY_DISCUSS, PHASE_VOTE):
        return

    cancel_phase_job(context, chat_id)
    game.phase = PHASE_VOTE
    reset_vote_state(game)

    await announce(
        context, chat_id,
        f"🗳 *Голосование ({VOTE_SECONDS} сек).* \nНажмите кнопку, кого выгоняем:"
    )

    # send voting keyboard to group (one message)
    kb = build_targets_keyboard(game, "vote")
    await announce(context, chat_id, "Выберите цель голосования:", reply_markup=kb)

    job_name = f"phase_{chat_id}"
    game.phase_job_name = job_name
    context.job_queue.run_once(
        vote_timeout_job,
        when=VOTE_SECONDS,
        name=job_name,
        data={"chat_id": chat_id},
    )


async def vote_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_vote(context, chat_id, timed_out=True)


async def resolve_vote(context: ContextTypes.DEFAULT_TYPE, chat_id: int, timed_out: bool):
    game = get_game(chat_id)
    if not game.active or game.phase != PHASE_VOTE:
        return

    cancel_phase_job(context, chat_id)

    if not game.vote_counts:
        await announce(context, chat_id, "🤷 Голосов нет. Никого не выгнали.")
        await start_night(context, chat_id)
        return

    # find max votes and ties
    max_votes = max(game.vote_counts.values())
    top = [uid for uid, c in game.vote_counts.items() if c == max_votes]
    top = [uid for uid in top if uid in game.players and game.players[uid].alive]

    if len(top) != 1:
        await announce(context, chat_id, f"⚖️ Ничья ({max_votes} голосов). Никого не выгнали.")
        await start_night(context, chat_id)
        return

    out_id = top[0]
    out_player = game.players[out_id]
    out_player.alive = False

    await announce(context, chat_id, f"🚪 По итогам голосования выгнали: *{out_player.name}*")

    winner = mafia_win_check(game)
    if winner:
        if winner == "mafia":
            await end_game(context, chat_id, "Мафия победила (мафии не меньше, чем мирных).")
        elif winner == "citizens":
            await end_game(context, chat_id, "Мирные победили (вся мафия устранена).")
        return

    await start_night(context, chat_id)


async def maybe_end_night_early(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """If all required roles acted, resolve night early."""
    game = get_game(chat_id)
    if not game.active or game.phase != PHASE_NIGHT:
        return

    mafia_ids = game.role_ids(ROLE_MAFIA)
    need_mafia = len(mafia_ids) > 0
    mafia_done = (game.mafia_target is not None) if need_mafia else True

    need_doc = len(game.role_ids(ROLE_DOCTOR)) > 0
    doc_done = game.doctor_acted if need_doc else True

    need_det = len(game.role_ids(ROLE_DETECTIVE)) > 0
    det_done = game.detective_acted if need_det else True

    if mafia_done and doc_done and det_done:
        await resolve_night(context, chat_id, timed_out=False)


# =========================
# CALLBACKS (INLINE BUTTONS)
# =========================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "noop":
        return

    if ":" not in data:
        return

    action, target_str = data.split(":", 1)
    try:
        target_id = int(target_str)
    except ValueError:
        return

    chat_id = query.message.chat_id  # for group message callbacks, this is group id
    user = query.from_user

    # IMPORTANT:
    # Night actions happen in DM, so chat_id here will be user's id (private chat)
    # Vote happens in group message, so chat_id is group id
    #
    # We need to detect where to apply it:
    if query.message.chat.type == "private":
        # find which group game this user belongs to
        # simplest: choose the first active game where user is a player
        target_game_chat_id = None
        for gid, g in games.items():
            if g.active and user.id in g.players:
                target_game_chat_id = gid
                break
        if target_game_chat_id is None:
            await query.edit_message_text("❌ Игра не найдена или уже завершена.")
            return
        game_chat_id = target_game_chat_id
    else:
        game_chat_id = chat_id

    game = get_game(game_chat_id)
    if not game.active:
        await query.edit_message_text("❌ Игра не активна.")
        return

    # validate target is alive player
    if target_id not in game.players or not game.players[target_id].alive:
        await query.edit_message_text("❌ Цель недоступна.")
        return

    # NIGHT ACTIONS
    if action in ("kill", "save", "check"):
        if game.phase != PHASE_NIGHT:
            await query.edit_message_text("⏳ Сейчас не ночь.")
            return

        if user.id not in game.players or not game.players[user.id].alive:
            await query.edit_message_text("❌ Ты не участвуешь или уже выбыл.")
            return

        my_role = game.players[user.id].role

        if action == "kill":
            if my_role != ROLE_MAFIA:
                await query.edit_message_text("❌ Ты не мафия.")
                return
            game.mafia_target = target_id
            game.mafia_voters.add(user.id)
            await query.edit_message_text(f"✅ Выбрано: убрать *{game.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)
            await maybe_end_night_early(context, game_chat_id)
            return

        if action == "save":
            if my_role != ROLE_DOCTOR:
                await query.edit_message_text("❌ Ты не доктор.")
                return
            game.doctor_save = target_id
            game.doctor_acted = True
            await query.edit_message_text(f"✅ Выбрано: лечить *{game.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)
            await maybe_end_night_early(context, game_chat_id)
            return

        if action == "check":
            if my_role != ROLE_DETECTIVE:
                await query.edit_message_text("❌ Ты не комиссар.")
                return
            game.detective_check = target_id
            game.detective_acted = True
            await query.edit_message_text(f"✅ Выбрано: проверить *{game.players[target_id].name}*", parse_mode=ParseMode.MARKDOWN)
            await maybe_end_night_early(context, game_chat_id)
            return

    # VOTE ACTION (GROUP)
    if action == "vote":
        if game.phase != PHASE_VOTE:
            await query.answer("Сейчас не голосование.", show_alert=True)
            return

        if user.id not in game.players or not game.players[user.id].alive:
            await query.answer("Ты не в игре или уже выбыл.", show_alert=True)
            return

        if user.id in game.voted_by:
            await query.answer("Ты уже голосовал(а).", show_alert=True)
            return

        game.voted_by.add(user.id)
        game.vote_counts[target_id] = game.vote_counts.get(target_id, 0) + 1

        # show vote status quickly in alert
        await query.answer(f"Голос принят ✅ ({game.players[target_id].name})", show_alert=False)

        # if everyone alive voted -> resolve early
        if len(game.voted_by) >= len(game.alive_ids()):
            await resolve_vote(context, game_chat_id, timed_out=False)
        return


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎩 *GAP Мафия*\n\n"
        "Команды:\n"
        "/newgame — создать игру\n"
        "/join — вступить\n"
        "/leave — выйти из лобби\n"
        "/players — список игроков\n"
        "/startgame — начать\n"
        "/status — статус\n"
        "/endgame — завершить\n",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)

    if game.active:
        await update.message.reply_text("❌ Игра уже идёт. /endgame чтобы завершить.")
        return

    game.players.clear()
    game.active = False
    game.phase = PHASE_WAITING
    reset_night_state(game)
    reset_vote_state(game)

    await update.message.reply_text(
        f"🎲 Создано лобби. Минимум игроков: *{MIN_PLAYERS}*\n"
        "Пусть люди нажмут /join",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = get_game(chat_id)

    if game.active:
        await update.message.reply_text("❌ Игра уже началась.")
        return

    if user.id in game.players:
        await update.message.reply_text("Ты уже в лобби 😎")
        return

    game.players[user.id] = Player(user_id=user.id, name=user.first_name)
    await update.message.reply_text(f"✅ {user.first_name} в игре. Сейчас игроков: {len(game.players)}")


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    game = get_game(chat_id)

    if game.active:
        await update.message.reply_text("❌ Нельзя выйти, игра уже идёт. /endgame для остановки.")
        return

    if user.id not in game.players:
        await update.message.reply_text("Тебя нет в лобби.")
        return

    game.players.pop(user.id, None)
    await update.message.reply_text(f"🚪 {user.first_name} вышел. Игроков: {len(game.players)}")


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)

    await update.message.reply_text(
        f"👥 Игроки:\n{format_alive_list(game)}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)

    txt = (
        f"📌 *Статус*\n"
        f"Активна: {'✅' if game.active else '❌'}\n"
        f"Фаза: *{game.phase}*\n"
        f"Игроков: {len(game.players)} (живых: {len(game.alive_ids())})\n\n"
        f"*Список:*\n{format_alive_list(game)}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)

    if game.active:
        await update.message.reply_text("❌ Игра уже идёт.")
        return

    if len(game.players) < MIN_PLAYERS:
        await update.message.reply_text(f"❌ Нужно минимум {MIN_PLAYERS} игроков.")
        return

    ok = pick_roles(game)
    if not ok:
        await update.message.reply_text(f"❌ Нужно минимум {MIN_PLAYERS} игроков.")
        return

    game.active = True
    game.phase = PHASE_NIGHT

    # DM roles
    dm_failed = 0
    for p in game.players.values():
        role = p.role or "?"
        role_txt = {
            ROLE_MAFIA: "😈 *МАФИЯ* — ночью выбираешь жертву.",
            ROLE_DOCTOR: "🩺 *ДОКТОР* — ночью лечишь одного игрока.",
            ROLE_DETECTIVE: "🕵️ *КОМИССАР* — ночью проверяешь одного игрока.",
            ROLE_CITIZEN: "🙂 *МИРНЫЙ* — ищешь мафию.",
        }.get(role, role)

        ok_dm = await safe_dm(context, p.user_id, f"🎭 Твоя роль: {role_txt}")
        if not ok_dm:
            dm_failed += 1

    await update.message.reply_text(
        "✅ Игра началась!\n"
        "Роли отправлены в личку (если кто-то не получит — пусть нажмёт Start в личке бота)."
        + (f"\n⚠️ Не удалось отправить в личку: {dm_failed} игрокам." if dm_failed else "")
    )

    await start_night(context, chat_id)


async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await end_game(context, chat_id, "Остановлено командой /endgame")


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("leave", cmd_leave))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("GAP Mafia bot is running (polling)")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )


if __name__ == "__main__":
    main()
