
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("TOKEN")

# =========================
# CONFIG
# =========================


MIN_PLAYERS = 5
NIGHT_SECONDS = 75
DAY_SECONDS = 120
VOTE_SECONDS = 60

# Roles
ROLE_MAFIA = "mafia"
ROLE_DON = "don"
ROLE_DOCTOR = "doctor"
ROLE_SHERIFF = "sheriff"          # комиссар
ROLE_MANIAC = "maniac"
ROLE_ESCORT = "escort"            # путана (блок)
ROLE_BODYGUARD = "bodyguard"
ROLE_LAWYER = "lawyer"            # 1 раз: иммунитет от дневной казни выбранному
ROLE_HOBO = "hobo"                # видит, кто приходил к цели ночью
ROLE_CIVILIAN = "civilian"

MAFIA_TEAM = {ROLE_MAFIA, ROLE_DON}


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
    phase: str = "idle"  # idle|lobby|night|day|voting|ended
    day_num: int = 0
    night_num: int = 0

    players: Dict[int, Player] = field(default_factory=dict)

    mafia_ids: Set[int] = field(default_factory=set)
    don_id: Optional[int] = None
    doctor_id: Optional[int] = None
    sheriff_id: Optional[int] = None
    maniac_id: Optional[int] = None
    escort_id: Optional[int] = None
    bodyguard_id: Optional[int] = None
    lawyer_id: Optional[int] = None
    hobo_id: Optional[int] = None

    # limits
    doctor_self_heal_used: bool = False
    lawyer_used: bool = False

    # night actions
    mafia_votes: Dict[int, int] = field(default_factory=dict)  # mafia_member -> target
    maniac_kill: Optional[int] = None
    heal_target: Optional[int] = None
    sheriff_check: Optional[int] = None
    escort_block: Optional[int] = None
    bodyguard_protect: Optional[int] = None
    lawyer_protect: Optional[int] = None  # immunity from vote next day
    hobo_watch: Optional[int] = None

    # day voting
    day_votes: Dict[int, int] = field(default_factory=dict)
    protected_from_vote: Optional[int] = None  # set by lawyer for the day

    # visitors log for hobo (target -> set(visitors))
    visitors: Dict[int, Set[int]] = field(default_factory=dict)

    # message ids
    night_msg_ids: List[int] = field(default_factory=list)
    vote_msg_id: Optional[int] = None

    # jobs
    night_job_name: Optional[str] = None
    day_job_name: Optional[str] = None
    vote_job_name: Optional[str] = None

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def role_alive(self, uid: Optional[int]) -> bool:
        return bool(uid and uid in self.players and self.players[uid].alive)

    def mafia_alive_ids(self) -> List[int]:
        return [uid for uid in self.mafia_ids if self.players.get(uid) and self.players[uid].alive]

    def alive_count_by_sides(self) -> Tuple[int, int, int]:
        mafia_alive = sum(1 for uid in self.mafia_ids if self.players.get(uid) and self.players[uid].alive)
        maniac_alive = 1 if (self.maniac_id and self.players.get(self.maniac_id) and self.players[self.maniac_id].alive) else 0
        town_alive = sum(
            1 for p in self.players.values()
            if p.alive and p.user_id not in self.mafia_ids and p.user_id != self.maniac_id
        )
        return mafia_alive, maniac_alive, town_alive

    def check_win(self) -> Optional[str]:
        mafia_alive, maniac_alive, town_alive = self.alive_count_by_sides()
        alive_total = mafia_alive + maniac_alive + town_alive

        if alive_total == 0:
            return "Игра закончилась (никого не осталось)."

        # Town win
        if mafia_alive == 0 and maniac_alive == 0:
            return "🎉 Мирные победили! Мафия и маньяк устранены."

        # Mafia win (mafia >= others)
        if mafia_alive > 0 and mafia_alive >= (town_alive + maniac_alive):
            return "💀 Мафия победила! Мафии стало не меньше, чем остальных."

        # Maniac win (only maniac left)
        if maniac_alive == 1 and mafia_alive == 0 and town_alive == 0:
            return "🩸 Маньяк победил! Он остался один."

        return None


GAMES: Dict[int, Game] = {}


def get_game(chat_id: int) -> Game:
    if chat_id not in GAMES:
        GAMES[chat_id] = Game(chat_id=chat_id)
    return GAMES[chat_id]


def group_only(update: Update) -> bool:
    c = update.effective_chat
    return c and c.type in ("group", "supergroup")


def role_ru(role: str) -> str:
    return {
        ROLE_MAFIA: "Мафия",
        ROLE_DON: "Дон",
        ROLE_DOCTOR: "Доктор",
        ROLE_SHERIFF: "Комиссар",
        ROLE_MANIAC: "Маньяк",
        ROLE_ESCORT: "Путана",
        ROLE_BODYGUARD: "Телохранитель",
        ROLE_LAWYER: "Адвокат",
        ROLE_HOBO: "Бомж",
        ROLE_CIVILIAN: "Мирный",
    }.get(role, role)


def plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= (n % 100) <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


# =========================
# UI
# =========================
def kb_targets(chat_id: int, action: str, g: Game, actor_uid: Optional[int] = None, allow_self: bool = True) -> InlineKeyboardMarkup:
    rows = []
    for p in g.alive_players():
        if actor_uid and not allow_self and p.user_id == actor_uid:
            continue
        rows.append([InlineKeyboardButton(p.name, callback_data=f"{chat_id}|{action}|{p.user_id}")])
    return InlineKeyboardMarkup(rows)


def cancel_jobs(context: ContextTypes.DEFAULT_TYPE, g: Game):
    for name in (g.night_job_name, g.day_job_name, g.vote_job_name):
        if name:
            for j in context.job_queue.get_jobs_by_name(name):
                j.schedule_removal()
    g.night_job_name = g.day_job_name = g.vote_job_name = None


async def safe_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> bool:
    """Try to DM; return False if user hasn't /start-ed the bot."""
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception:
        return False


# =========================
# ROLE DISTRIBUTION (AUTO)
# =========================
def build_roles(n: int) -> List[str]:
    """
    “Все роли” включаются по числу игроков (чтобы не ломать баланс):
    5-6: Дон, Доктор, Комиссар, остальные мирные
    7-8: Дон, Мафия(1), Доктор, Комиссар, Маньяк
    9-10: Дон, Мафия(2), Доктор, Комиссар, Маньяк, Путана
    11-12: Дон, Мафия(2), Доктор, Комиссар, Маньяк, Путана, Телохранитель, Адвокат
    13+: Дон, Мафия(3), Доктор, Комиссар, Маньяк, Путана, Телохранитель, Адвокат, Бомж
    """
    if n <= 6:
        base = [ROLE_DON, ROLE_DOCTOR, ROLE_SHERIFF]
    elif n <= 8:
        base = [ROLE_DON, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF, ROLE_MANIAC]
    elif n <= 10:
        base = [ROLE_DON, ROLE_MAFIA, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF, ROLE_MANIAC, ROLE_ESCORT]
    elif n <= 12:
        base = [ROLE_DON, ROLE_MAFIA, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF, ROLE_MANIAC, ROLE_ESCORT, ROLE_BODYGUARD, ROLE_LAWYER]
    else:
        base = [ROLE_DON, ROLE_MAFIA, ROLE_MAFIA, ROLE_MAFIA, ROLE_DOCTOR, ROLE_SHERIFF, ROLE_MANIAC,
                ROLE_ESCORT, ROLE_BODYGUARD, ROLE_LAWYER, ROLE_HOBO]

    base = base[:n]
    base += [ROLE_CIVILIAN] * (n - len(base))
    random.shuffle(base)
    return base


# =========================
# COMMANDS: LOBBY
# =========================
async def mafia_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return

    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    cancel_jobs(context, g)

    # reset
    g.phase = "lobby"
    g.day_num = g.night_num = 0
    g.players = {}
    g.mafia_ids = set()
    g.don_id = g.doctor_id = g.sheriff_id = g.maniac_id = g.escort_id = g.bodyguard_id = g.lawyer_id = g.hobo_id = None
    g.doctor_self_heal_used = False
    g.lawyer_used = False
    g.protected_from_vote = None

    await update.message.reply_text(
        "🕵️‍♀️ *Мафия — набор открыт!*\n\n"
        "• /join — присоединиться\n"
        "• /leave — выйти\n"
        "• /begin — начать\n"
        "• /stop — сброс\n\n"
        "⚠️ Каждый игрок должен открыть бота в личке и нажать Start, чтобы получить роль.",
        parse_mode="Markdown"
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Вступать нужно в группе.")
        return

    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    if g.phase != "lobby":
        await update.message.reply_text("Набор закрыт. /mafia_start для новой игры.")
        return

    user = update.effective_user
    if user.id in g.players:
        await update.message.reply_text("Ты уже в игре 🙂")
        return

    g.players[user.id] = Player(user_id=user.id, name=user.full_name)
    n = len(g.players)
    await update.message.reply_text(f"✅ {user.full_name} в игре. Сейчас {n} {plural(n,'игрок','игрока','игроков')}.")


async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return

    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    user = update.effective_user

    if g.phase != "lobby":
        await update.message.reply_text("Во время игры выйти нельзя. Используй /stop.")
        return

    if user.id not in g.players:
        await update.message.reply_text("Тебя нет в списке игроков.")
        return

    del g.players[user.id]
    n = len(g.players)
    await update.message.reply_text(f"❌ {user.full_name} вышел(ла). Сейчас {n} {plural(n,'игрок','игрока','игроков')}.")


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return

    chat_id = update.effective_chat.id
    g = get_game(chat_id)

    if g.phase != "lobby":
        await update.message.reply_text("Игра уже идёт или не открыта. /mafia_start для новой.")
        return

    if len(g.players) < MIN_PLAYERS:
        await update.message.reply_text(f"Нужно минимум {MIN_PLAYERS} игроков.")
        return

    ids = list(g.players.keys())
    random.shuffle(ids)
    roles = build_roles(len(ids))

    # reset role maps
    g.mafia_ids = set()
    g.don_id = g.doctor_id = g.sheriff_id = g.maniac_id = g.escort_id = g.bodyguard_id = g.lawyer_id = g.hobo_id = None
    g.doctor_self_heal_used = False
    g.lawyer_used = False
    g.protected_from_vote = None

    # assign
    for uid, r in zip(ids, roles):
        g.players[uid].role = r
        if r in MAFIA_TEAM:
            g.mafia_ids.add(uid)
        if r == ROLE_DON: g.don_id = uid
        elif r == ROLE_DOCTOR: g.doctor_id = uid
        elif r == ROLE_SHERIFF: g.sheriff_id = uid
        elif r == ROLE_MANIAC: g.maniac_id = uid
        elif r == ROLE_ESCORT: g.escort_id = uid
        elif r == ROLE_BODYGUARD: g.bodyguard_id = uid
        elif r == ROLE_LAWYER: g.lawyer_id = uid
        elif r == ROLE_HOBO: g.hobo_id = uid

    names = ", ".join(p.name for p in g.players.values())
    await update.message.reply_text(
        f"🎬 *Игра началась!*\nИгроки: {names}\n\nОтправляю роли в личку…",
        parse_mode="Markdown"
    )

    # DM roles
    failed = []
    for uid in ids:
        r = g.players[uid].role or "?"
        ok = await safe_dm(
            context,
            uid,
            f"🎭 Твоя роль: {role_ru(r)}\n"
            "Не показывай никому.\n"
            "Ночные действия — кнопками в общем чате."
        )
        if not ok:
            failed.append(g.players[uid].name)

    if failed:
        await update.message.reply_text(
            "⚠️ Не смог(ла) отправить роль в личку этим игрокам:\n"
            + "\n".join(f"• {n}" for n in failed) +
            "\n\nПусть каждый откроет бота в личке и нажмёт Start, затем перезапустите игру /stop и /mafia_start.",
        )

    await start_night(chat_id, context)


# =========================
# NIGHT HELPERS
# =========================
def track_visit(g: Game, visitor: Optional[int], target: Optional[int]):
    if visitor is None or target is None:
        return
    if visitor not in g.players or target not in g.players:
        return
    if not g.players[visitor].alive or not g.players[target].alive:
        return
    g.visitors.setdefault(target, set()).add(visitor)


def is_blocked(g: Game, uid: Optional[int]) -> bool:
    return bool(uid and g.escort_block == uid and g.role_alive(g.escort_id))


def mafia_kill_target(g: Game) -> Optional[int]:
    votes = [t for uid, t in g.mafia_votes.items() if uid in g.mafia_ids and g.players.get(uid) and g.players[uid].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [t for t, c in counts.items() if c == mx]
    return random.choice(top)


def all_ready(g: Game) -> bool:
    mafia_alive = g.mafia_alive_ids()
    mafia_ready = (len(mafia_alive) == 0) or all(uid in g.mafia_votes for uid in mafia_alive)

    doctor_ready = (not g.role_alive(g.doctor_id)) or (g.heal_target is not None)
    sheriff_ready = (not g.role_alive(g.sheriff_id)) or (g.sheriff_check is not None)
    maniac_ready = (not g.role_alive(g.maniac_id)) or (g.maniac_kill is not None)
    escort_ready = (not g.role_alive(g.escort_id)) or (g.escort_block is not None)
    guard_ready = (not g.role_alive(g.bodyguard_id)) or (g.bodyguard_protect is not None)
    lawyer_ready = (not g.role_alive(g.lawyer_id)) or (g.lawyer_used or (g.lawyer_protect is not None))
    hobo_ready = (not g.role_alive(g.hobo_id)) or (g.hobo_watch is not None)

    return mafia_ready and doctor_ready and sheriff_ready and maniac_ready and escort_ready and guard_ready and lawyer_ready and hobo_ready


# =========================
# NIGHT / DAY / VOTE FLOW
# =========================
async def start_night(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    cancel_jobs(context, g)

    g.phase = "night"
    g.night_num += 1

    # reset night
    g.mafia_votes = {}
    g.maniac_kill = None
    g.heal_target = None
    g.sheriff_check = None
    g.escort_block = None
    g.bodyguard_protect = None
    g.lawyer_protect = None
    g.hobo_watch = None
    g.visitors = {}
    g.night_msg_ids = []

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 *НОЧЬ {g.night_num}* — роли делают действия. ⏳ {NIGHT_SECONDS} сек.",
        parse_mode="Markdown"
    )

    mids = []

    if len(g.mafia_alive_ids()) > 0:
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="🔫 Мафия/Дон: выберите жертву (каждый голосует, решает большинство).",
            reply_markup=kb_targets(chat_id, "KILL", g, actor_uid=None, allow_self=False)
        )
        mids.append(m.message_id)

    if g.role_alive(g.maniac_id):
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="🩸 Маньяк: выбери жертву.",
            reply_markup=kb_targets(chat_id, "MKILL", g, actor_uid=g.maniac_id, allow_self=False)
        )
        mids.append(m.message_id)

    if g.role_alive(g.doctor_id):
        note = "💉 Доктор: выбери, кого лечить."
        note += " (самолечение 1 раз)" if not g.doctor_self_heal_used else " (самолечение уже использовано)"
        m = await context.bot.send_message(
            chat_id=chat_id,
            text=note,
            reply_markup=kb_targets(chat_id, "HEAL", g, actor_uid=g.doctor_id, allow_self=not g.doctor_self_heal_used)
        )
        mids.append(m.message_id)

    if g.role_alive(g.sheriff_id):
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="🕵️ Комиссар: выбери, кого проверить (результат придёт в личку).",
            reply_markup=kb_targets(chat_id, "CHECK", g, actor_uid=g.sheriff_id, allow_self=False)
        )
        mids.append(m.message_id)

    if g.role_alive(g.escort_id):
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="💃 Путана: выбери, кого блокировать (его роль не сработает).",
            reply_markup=kb_targets(chat_id, "BLOCK", g, actor_uid=g.escort_id, allow_self=False)
        )
        mids.append(m.message_id)

    if g.role_alive(g.bodyguard_id):
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="🛡️ Телохранитель: выбери, кого защищать (может погибнуть вместо цели).",
            reply_markup=kb_targets(chat_id, "GUARD", g, actor_uid=g.bodyguard_id, allow_self=False)
        )
        mids.append(m.message_id)

    if g.role_alive(g.lawyer_id):
        if g.lawyer_used:
            await context.bot.send_message(chat_id=chat_id, text="⚖️ Адвокат: способность уже использована (1 раз за игру).")
        else:
            m = await context.bot.send_message(
                chat_id=chat_id,
                text="⚖️ Адвокат: выбери игрока — он будет защищён от дневной казни (результат/подтверждение в личку).",
                reply_markup=kb_targets(chat_id, "LAW", g, actor_uid=g.lawyer_id, allow_self=True)
            )
            mids.append(m.message_id)

    if g.role_alive(g.hobo_id):
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="🧥 Бомж: выбери игрока — увидишь, кто к нему приходил (результат в личку утром).",
            reply_markup=kb_targets(chat_id, "WATCH", g, actor_uid=g.hobo_id, allow_self=False)
        )
        mids.append(m.message_id)

    g.night_msg_ids = mids

    g.night_job_name = f"night_end_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(night_timeout, when=NIGHT_SECONDS, name=g.night_job_name, data={"chat_id": chat_id})


async def night_timeout(context: ContextTypes.DEFAULT_TYPE):
    await resolve_night(context.job.data["chat_id"], context, forced=True)


async def resolve_night(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced: bool):
    g = get_game(chat_id)
    if g.phase != "night":
        return

    # disable keyboards
    for mid in g.night_msg_ids:
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=None)
        except Exception:
            pass
    g.night_msg_ids = []

    if forced:
        await context.bot.send_message(chat_id=chat_id, text="⏰ Ночь закончилась (таймер).")

    # collect actions
    mafia_kill = mafia_kill_target(g)
    maniac_kill = g.maniac_kill
    heal = g.heal_target
    guard = g.bodyguard_protect
    law = g.lawyer_protect
    watch = g.hobo_watch
    check = g.sheriff_check

    # blocks cancel actions
    if is_blocked(g, g.maniac_id): maniac_kill = None
    if is_blocked(g, g.doctor_id): heal = None
    if is_blocked(g, g.bodyguard_id): guard = None
    if is_blocked(g, g.lawyer_id): law = None
    if is_blocked(g, g.hobo_id): watch = None
    if is_blocked(g, g.sheriff_id): check = None

    # mafia kill canceled if escort blocked any alive mafia member (simple rule)
    if g.escort_block in g.mafia_ids and g.role_alive(g.escort_block):
        mafia_kill = None

    # track visits for hobo (before kills)
    if mafia_kill is not None:
        for uid in g.mafia_alive_ids():
            track_visit(g, uid, mafia_kill)
    if maniac_kill is not None:
        track_visit(g, g.maniac_id, maniac_kill)
    if heal is not None:
        track_visit(g, g.doctor_id, heal)
    if guard is not None:
        track_visit(g, g.bodyguard_id, guard)
    if law is not None and not g.lawyer_used:
        track_visit(g, g.lawyer_id, law)
    if watch is not None:
        track_visit(g, g.hobo_id, watch)
    if check is not None:
        track_visit(g, g.sheriff_id, check)

    # lawyer protection applies to next day
    g.protected_from_vote = None
    if law is not None and not g.lawyer_used:
        g.lawyer_used = True
        g.protected_from_vote = law
        await safe_dm(context, g.lawyer_id, f"⚖️ Ты защитил(а) от дневной казни: {g.players[law].name}")

    # bodyguard intercept
    deaths: Set[int] = set()

    def apply_attack(target: Optional[int]) -> Optional[int]:
        if target is None:
            return None
        if target not in g.players or not g.players[target].alive:
            return None
        if guard is not None and target == guard and g.role_alive(g.bodyguard_id):
            deaths.add(g.bodyguard_id)  # bodyguard dies
            return None
        return target

    mafia_target = apply_attack(mafia_kill)
    maniac_target = apply_attack(maniac_kill)

    # doctor heal cancels death
    if mafia_target is not None and (heal is None or heal != mafia_target):
        deaths.add(mafia_target)
    if maniac_target is not None and (heal is None or heal != maniac_target):
        deaths.add(maniac_target)

    killed_names = []
    for uid in list(deaths):
        if uid in g.players and g.players[uid].alive:
            g.players[uid].alive = False
            killed_names.append(g.players[uid].name)

    # sheriff result to DM (Don appears NOT mafia)
    if check is not None and g.role_alive(g.sheriff_id) and check in g.players:
        is_mafia = (check in g.mafia_ids) and (check != g.don_id)
        await safe_dm(
            context,
            g.sheriff_id,
            f"🕵️ Проверка: {g.players[check].name} — {'МАФИЯ' if is_mafia else 'НЕ мафия'}"
        )

    # hobo result to DM (names of visitors)
    if watch is not None and g.role_alive(g.hobo_id) and watch in g.players:
        visitors = g.visitors.get(watch, set())
        visitors = {uid for uid in visitors if uid != g.hobo_id}
        if not visitors:
            msg = f"🧥 Слежка: к {g.players[watch].name} этой ночью никто не приходил."
        else:
            names = ", ".join(g.players[uid].name for uid in visitors if uid in g.players)
            msg = f"🧥 Слежка: к {g.players[watch].name} приходили: {names}"
        await safe_dm(context, g.hobo_id, msg)

    # morning announcement
    if killed_names:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🌅 Утро. Ночью погибли: " + ", ".join(f"*{n}*" for n in killed_names) + ".",
            parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text="🌅 Утро. Ночью никто не погиб.")

    # win check
    win = g.check_win()
    if win:
        g.phase = "ended"
        await context.bot.send_message(chat_id=chat_id, text=win)
        await reveal_roles(chat_id, context)
        return

    # start day
    g.phase = "day"
    g.day_num += 1
    g.day_votes = {}

    extra = ""
    if g.protected_from_vote and g.players.get(g.protected_from_vote) and g.players[g.protected_from_vote].alive:
        extra = "⚖️ (Сегодня один игрок защищён от казни — кто именно не раскрывается.)\n"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"☀️ *ДЕНЬ {g.day_num}*\n{extra}Обсуждение: {DAY_SECONDS} сек. Затем авто-голосование. Можно вручную: /vote",
        parse_mode="Markdown"
    )

    cancel_jobs(context, g)
    g.day_job_name = f"day_to_vote_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(day_to_vote_timeout, when=DAY_SECONDS, name=g.day_job_name, data={"chat_id": chat_id})


async def day_to_vote_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    g = get_game(chat_id)
    if g.phase == "day":
        await start_vote(chat_id, context, auto=True)


# =========================
# VOTING
# =========================
async def vote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return
    await start_vote(update.effective_chat.id, context, auto=False)


async def start_vote(chat_id: int, context: ContextTypes.DEFAULT_TYPE, auto: bool):
    g = get_game(chat_id)
    if g.phase != "day":
        if not auto:
            await context.bot.send_message(chat_id=chat_id, text="Голосование можно начинать только днём.")
        return

    cancel_jobs(context, g)
    g.phase = "voting"
    g.day_votes = {}

    prefix = "🤖 Авто-голосование.\n" if auto else ""
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(prefix +
              "🗳️ *ГОЛОСОВАНИЕ*\n"
              f"⏳ {VOTE_SECONDS} сек. Последний голос учитывается.\n"
              "Нельзя голосовать за себя."),
        parse_mode="Markdown",
        reply_markup=kb_targets(chat_id, "VOTE", g, actor_uid=None, allow_self=False)
    )
    g.vote_msg_id = msg.message_id

    g.vote_job_name = f"vote_end_{chat_id}_{int(time.time())}"
    context.job_queue.run_once(vote_timeout, when=VOTE_SECONDS, name=g.vote_job_name, data={"chat_id": chat_id})


async def vote_timeout(context: ContextTypes.DEFAULT_TYPE):
    await resolve_vote(context.job.data["chat_id"], context, forced=True)


async def endvote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        return
    await resolve_vote(update.effective_chat.id, context, forced=True)


def vote_result(g: Game) -> Optional[int]:
    votes = [t for voter, t in g.day_votes.items() if g.players.get(voter) and g.players[voter].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [t for t, c in counts.items() if c == mx]
    return random.choice(top)


async def resolve_vote(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced: bool):
    g = get_game(chat_id)
    if g.phase != "voting":
        return

    if g.vote_msg_id:
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=g.vote_msg_id, reply_markup=None)
        except Exception:
            pass
        g.vote_msg_id = None

    if forced:
        await context.bot.send_message(chat_id=chat_id, text="⏰ Голосование завершено.")

    target = vote_result(g)
    if target is None or target not in g.players or not g.players[target].alive:
        await context.bot.send_message(chat_id=chat_id, text="Никого не выбрали. Начинается ночь.")
        await start_night(chat_id, context)
        return

    # lawyer protection
    if g.protected_from_vote == target and g.players[target].alive:
        await context.bot.send_message(chat_id=chat_id, text="⚖️ Попытка казни не удалась: игрок был защищён от казни сегодня.")
        await start_night(chat_id, context)
        return

    g.players[target].alive = False
    await context.bot.send_message(chat_id=chat_id, text=f"🚫 Исключён(а): *{g.players[target].name}*.", parse_mode="Markdown")

    win = g.check_win()
    if win:
        g.phase = "ended"
        await context.bot.send_message(chat_id=chat_id, text=win)
        await reveal_roles(chat_id, context)
        return

    await start_night(chat_id, context)


# =========================
# STATUS / STOP / REVEAL
# =========================
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return
    chat_id = update.effective_chat.id
    g = get_game(chat_id)

    if g.phase == "idle":
        await update.message.reply_text("Игры нет. /mafia_start")
        return

    alive = [p.name for p in g.alive_players()]
    dead = [p.name for p in g.players.values() if not p.alive]
    await update.message.reply_text(
        f"📌 Фаза: {g.phase}\n"
        f"🌙 Ночь: {g.night_num} | ☀️ День: {g.day_num}\n"
        f"🟢 Живые ({len(alive)}): " + (", ".join(alive) if alive else "—") + "\n"
        f"⚫ Выбыли ({len(dead)}): " + (", ".join(dead) if dead else "—")
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await update.message.reply_text("Команда работает только в группе.")
        return
    chat_id = update.effective_chat.id
    if chat_id in GAMES:
        g = GAMES[chat_id]
        cancel_jobs(context, g)
        del GAMES[chat_id]
    await update.message.reply_text("🛑 Игра остановлена и сброшена.")


async def reveal_roles(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    lines = ["🎭 *Роли игроков:*"]
    for p in g.players.values():
        status_icon = "🟢" if p.alive else "⚫"
        lines.append(f"{status_icon} {p.name} — *{role_ru(p.role or '?')}*")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")


# =========================
# CALLBACKS
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
    actor_uid = q.from_user.id
    actor = g.players.get(actor_uid)

    if not actor or not actor.alive:
        await q.answer("Ты не в игре или уже выбыл(а).", show_alert=True)
        return

    if target_uid not in g.players or not g.players[target_uid].alive:
        await q.answer("Цель недоступна.", show_alert=True)
        return

    # Night actions
    if action in ("KILL", "MKILL", "HEAL", "CHECK", "BLOCK", "GUARD", "LAW", "WATCH"):
        if g.phase != "night":
            await q.answer("Сейчас не ночь.", show_alert=True)
            return

        # Escort
        if action == "BLOCK":
            if g.escort_id != actor_uid:
                await q.answer("Ты не Путана.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя блокировать себя.", show_alert=True)
                return
            g.escort_block = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"💃 Ты блокируешь: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Mafia kill vote
        if action == "KILL":
            if actor_uid not in g.mafia_ids:
                await q.answer("Ты не мафия/дон.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя выбрать себя.", show_alert=True)
                return
            g.mafia_votes[actor_uid] = target_uid
            await q.answer("Голос принят.")
            await safe_dm(context, actor_uid, f"🔫 Твой голос: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Maniac kill
        if action == "MKILL":
            if g.maniac_id != actor_uid:
                await q.answer("Ты не Маньяк.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя выбрать себя.", show_alert=True)
                return
            g.maniac_kill = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"🩸 Твоя жертва: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Doctor heal
        if action == "HEAL":
            if g.doctor_id != actor_uid:
                await q.answer("Ты не Доктор.", show_alert=True)
                return
            if target_uid == actor_uid:
                if g.doctor_self_heal_used:
                    await q.answer("Самолечение уже использовано.", show_alert=True)
                    return
                g.doctor_self_heal_used = True
            g.heal_target = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"💉 Ты лечишь: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Sheriff check (result later in resolve_night; we just set target now)
        if action == "CHECK":
            if g.sheriff_id != actor_uid:
                await q.answer("Ты не Комиссар.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя проверять себя.", show_alert=True)
                return
            g.sheriff_check = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"🕵️ Ты проверяешь: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Bodyguard
        if action == "GUARD":
            if g.bodyguard_id != actor_uid:
                await q.answer("Ты не Телохранитель.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя защищать себя.", show_alert=True)
                return
            g.bodyguard_protect = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"🛡️ Ты защищаешь: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Lawyer one-time
        if action == "LAW":
            if g.lawyer_id != actor_uid:
                await q.answer("Ты не Адвокат.", show_alert=True)
                return
            if g.lawyer_used:
                await q.answer("Способность уже использована.", show_alert=True)
                return
            g.lawyer_protect = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"⚖️ Ты планируешь защитить от дневной казни: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        # Hobo watch (names later in resolve_night)
        if action == "WATCH":
            if g.hobo_id != actor_uid:
                await q.answer("Ты не Бомж.", show_alert=True)
                return
            if target_uid == actor_uid:
                await q.answer("Нельзя следить за собой.", show_alert=True)
                return
            g.hobo_watch = target_uid
            await q.answer("Выбор принят.")
            await safe_dm(context, actor_uid, f"🧥 Ты следишь за: {g.players[target_uid].name}")
            if all_ready(g):
                await resolve_night(chat_id, context, forced=False)
            return

        await q.answer()
        return

    # Voting
    if action == "VOTE":
        if g.phase != "voting":
            await q.answer("Сейчас нет голосования.", show_alert=True)
            return
        if target_uid == actor_uid:
            await q.answer("Нельзя голосовать за себя.", show_alert=True)
            return

        g.day_votes[actor_uid] = target_uid
        await q.answer("Голос принят.")

        # early majority
        alive_count = len(g.alive_players())
        needed = (alive_count // 2) + 1
        counts: Dict[int, int] = {}
        for voter, tgt in g.day_votes.items():
            if g.players.get(voter) and g.players[voter].alive:
                counts[tgt] = counts.get(tgt, 0) + 1
        for tgt, c in counts.items():
            if c >= needed:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Большинство ({c}/{alive_count}). Завершаю голосование.")
                await resolve_vote(chat_id, context, forced=False)
                return
        return

    await q.answer()


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("mafia_start", mafia_start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("leave", leave))
    app.add_handler(CommandHandler("begin", begin))

    app.add_handler(CommandHandler("vote", vote_cmd))
    app.add_handler(CommandHandler("endvote", endvote))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))

    app.add_handler(CallbackQueryHandler(on_button))

    print("Mafia bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
