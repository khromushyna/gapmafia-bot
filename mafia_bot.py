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
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# TOKEN (ONLY ENV)
# =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN env var is not set. Set it in Render -> Environment -> TOKEN")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("GAP-MAFIA")

# =========================
# ROLES
# =========================
ROLE_MAFIA = "mafia"
ROLE_DON = "don"
ROLE_DOCTOR = "doctor"
ROLE_SHERIFF = "sheriff"
ROLE_MANIAC = "maniac"
ROLE_ESCORT = "escort"
ROLE_BODYGUARD = "bodyguard"
ROLE_LAWYER = "lawyer"
ROLE_HOBO = "hobo"
ROLE_CIVILIAN = "civilian"

MAFIA_TEAM = {ROLE_MAFIA, ROLE_DON}

ROLE_RU = {
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
}

# =========================
# DEFAULT SETTINGS
# =========================
DEFAULT_MIN_PLAYERS = 5
DEFAULT_NIGHT_SECONDS = 60
DEFAULT_DAY_SECONDS = 90
DEFAULT_VOTE_SECONDS = 60

# Options
DEFAULT_REVEAL_ROLE_ON_LYNCH = True
DEFAULT_REVEAL_ROLE_ON_NIGHT_DEATH = False

DEFAULT_TIE_POLICY = "random"  # random|nobody|revote

DEFAULT_DOCTOR_SELF_HEAL_LIMIT = 1
DEFAULT_DOCTOR_NO_REPEAT_TARGET = True
DEFAULT_ESCORT_NO_REPEAT_TARGET = True

# =========================
# MODELS
# =========================
@dataclass
class Player:
    user_id: int
    name: str
    alive: bool = True
    role: Optional[str] = None


@dataclass
class Config:
    min_players: int = DEFAULT_MIN_PLAYERS
    night_seconds: int = DEFAULT_NIGHT_SECONDS
    day_seconds: int = DEFAULT_DAY_SECONDS
    vote_seconds: int = DEFAULT_VOTE_SECONDS

    reveal_role_on_lynch: bool = DEFAULT_REVEAL_ROLE_ON_LYNCH
    reveal_role_on_night_death: bool = DEFAULT_REVEAL_ROLE_ON_NIGHT_DEATH

    tie_policy: str = DEFAULT_TIE_POLICY

    doctor_self_heal_limit: int = DEFAULT_DOCTOR_SELF_HEAL_LIMIT
    doctor_no_repeat_target: bool = DEFAULT_DOCTOR_NO_REPEAT_TARGET
    escort_no_repeat_target: bool = DEFAULT_ESCORT_NO_REPEAT_TARGET


@dataclass
class CustomRoles:
    # counts; if 0 => disabled
    don: int = 1
    mafia: int = 1
    doctor: int = 1
    sheriff: int = 1
    maniac: int = 0
    escort: int = 0
    bodyguard: int = 0
    lawyer: int = 0
    hobo: int = 0


@dataclass
class Game:
    chat_id: int
    phase: str = "idle"  # idle|lobby|night|day|voting|ended
    paused: bool = False

    mode: str = "classic"  # classic|full|custom
    cfg: Config = field(default_factory=Config)
    custom: CustomRoles = field(default_factory=CustomRoles)

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

    # night actions
    mafia_votes: Dict[int, int] = field(default_factory=dict)   # mafia_member -> target
    maniac_kill: Optional[int] = None
    heal_target: Optional[int] = None
    sheriff_check: Optional[int] = None
    escort_block: Optional[int] = None
    bodyguard_protect: Optional[int] = None
    lawyer_protect: Optional[int] = None
    hobo_watch: Optional[int] = None

    # restrictions memory
    doctor_self_heals_used: int = 0
    doctor_last_target: Optional[int] = None
    escort_last_target: Optional[int] = None

    lawyer_used: bool = False
    protected_from_vote: Optional[int] = None

    visitors: Dict[int, Set[int]] = field(default_factory=dict)

    day_votes: Dict[int, int] = field(default_factory=dict)

    # UI
    lobby_msg_id: Optional[int] = None
    night_status_msg_id: Optional[int] = None

    # history
    log_lines: List[str] = field(default_factory=list)

    # job name
    job_name: Optional[str] = None

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def role_alive(self, uid: Optional[int]) -> bool:
        return bool(uid and uid in self.players and self.players[uid].alive)

    def mafia_alive_ids(self) -> List[int]:
        return [uid for uid in self.mafia_ids if uid in self.players and self.players[uid].alive]

    def alive_count_by_sides(self) -> Tuple[int, int, int]:
        mafia_alive = sum(1 for uid in self.mafia_ids if uid in self.players and self.players[uid].alive)
        maniac_alive = 1 if self.maniac_id and self.maniac_id in self.players and self.players[self.maniac_id].alive else 0
        town_alive = sum(
            1 for p in self.players.values()
            if p.alive and p.user_id not in self.mafia_ids and p.user_id != self.maniac_id
        )
        return mafia_alive, maniac_alive, town_alive

    def check_win(self) -> Optional[str]:
        mafia_alive, maniac_alive, town_alive = self.alive_count_by_sides()
        if mafia_alive == 0 and maniac_alive == 0:
            return "🎉 *Мирные победили!* Мафия и маньяк устранены."
        if mafia_alive > 0 and mafia_alive >= (town_alive + maniac_alive):
            return "💀 *Мафия победила!* Мафии стало не меньше, чем остальных."
        if maniac_alive == 1 and mafia_alive == 0 and town_alive == 0:
            return "🩸 *Маньяк победил!* Он остался один."
        return None


GAMES: Dict[int, Game] = {}

# =========================
# HELPERS
# =========================
def is_group(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type in (ChatType.GROUP, ChatType.SUPERGROUP))

def get_game(chat_id: int) -> Game:
    if chat_id not in GAMES:
        GAMES[chat_id] = Game(chat_id=chat_id)
    return GAMES[chat_id]

def is_adminish(update: Update) -> bool:
    # simple gate: chat admins only. If fails to detect, we allow (Telegram limits).
    # For real strict mode, fetch chat administrators (slower).
    return True

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

def cancel_job(context: ContextTypes.DEFAULT_TYPE, g: Game):
    if g.job_name:
        for j in context.job_queue.get_jobs_by_name(g.job_name):
            j.schedule_removal()
        g.job_name = None

def schedule_job(context: ContextTypes.DEFAULT_TYPE, g: Game, seconds: int, cb, suffix: str):
    cancel_job(context, g)
    g.job_name = f"{suffix}_{g.chat_id}_{int(time.time())}"
    context.job_queue.run_once(cb, when=seconds, name=g.job_name, data={"chat_id": g.chat_id})

def names_list(g: Game, only_alive: bool = False) -> str:
    ps = g.alive_players() if only_alive else list(g.players.values())
    return ", ".join(p.name for p in ps) if ps else "—"

def role_name(role: Optional[str]) -> str:
    if not role:
        return "?"
    return ROLE_RU.get(role, role)

def track(g: Game, line: str):
    g.log_lines.append(line)

def kb_lobby(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎭 Присоединиться", callback_data=f"LOBBY_JOIN:{chat_id}")],
        [InlineKeyboardButton("🚪 Выйти", callback_data=f"LOBBY_LEAVE:{chat_id}")],
        [InlineKeyboardButton("📋 Список", callback_data=f"LOBBY_LIST:{chat_id}")],
        [InlineKeyboardButton("▶️ Начать", callback_data=f"LOBBY_BEGIN:{chat_id}")],
        [
            InlineKeyboardButton("⚙️ Classic", callback_data=f"LOBBY_MODE:classic:{chat_id}"),
            InlineKeyboardButton("⚙️ Full", callback_data=f"LOBBY_MODE:full:{chat_id}"),
            InlineKeyboardButton("⚙️ Custom", callback_data=f"LOBBY_MODE:custom:{chat_id}"),
        ],
        [InlineKeyboardButton("🛑 Сброс", callback_data=f"LOBBY_STOP:{chat_id}")],
    ])

def kb_targets(chat_id: int, action: str, g: Game, exclude_uid: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = []
    for p in g.alive_players():
        if exclude_uid is not None and p.user_id == exclude_uid:
            continue
        rows.append([InlineKeyboardButton(p.name, callback_data=f"{action}:{chat_id}:{p.user_id}")])
    rows.append([InlineKeyboardButton("⏭ Пропустить", callback_data=f"{action}:{chat_id}:0")])
    return InlineKeyboardMarkup(rows)

def kb_vote(chat_id: int, g: Game, exclude_uid: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = []
    for p in g.alive_players():
        if exclude_uid is not None and p.user_id == exclude_uid:
            continue
        rows.append([InlineKeyboardButton(f"🗳 {p.name}", callback_data=f"VOTE:{chat_id}:{p.user_id}")])
    rows.append([InlineKeyboardButton("🙅 Снять голос", callback_data=f"VOTE:{chat_id}:0")])
    rows.append([InlineKeyboardButton("📊 Статус", callback_data=f"STATUSBTN:{chat_id}:0")])
    return InlineKeyboardMarkup(rows)

def track_visit(g: Game, visitor: Optional[int], target: Optional[int]):
    if not visitor or not target:
        return
    if visitor not in g.players or target not in g.players:
        return
    if not g.players[visitor].alive or not g.players[target].alive:
        return
    g.visitors.setdefault(target, set()).add(visitor)

def mafia_kill_target(g: Game) -> Optional[int]:
    votes = [t for uid, t in g.mafia_votes.items() if uid in g.mafia_ids and uid in g.players and g.players[uid].alive and t in g.players and g.players[t].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [t for t, c in counts.items() if c == mx]
    return random.choice(top)

def is_blocked(g: Game, uid: Optional[int]) -> bool:
    return bool(uid and g.escort_block == uid and g.role_alive(g.escort_id))

def build_roles_classic(n: int) -> List[str]:
    mafia_count = 1 if n <= 7 else (2 if n <= 11 else 3)
    base = [ROLE_DON] + [ROLE_MAFIA] * mafia_count + [ROLE_DOCTOR, ROLE_SHERIFF]
    base = base[:n]
    base += [ROLE_CIVILIAN] * (n - len(base))
    random.shuffle(base)
    return base

def build_roles_full(n: int) -> List[str]:
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

def build_roles_custom(n: int, custom: CustomRoles) -> List[str]:
    base: List[str] = []
    base += [ROLE_DON] * max(0, custom.don)
    base += [ROLE_MAFIA] * max(0, custom.mafia)
    base += [ROLE_DOCTOR] * max(0, custom.doctor)
    base += [ROLE_SHERIFF] * max(0, custom.sheriff)
    base += [ROLE_MANIAC] * max(0, custom.maniac)
    base += [ROLE_ESCORT] * max(0, custom.escort)
    base += [ROLE_BODYGUARD] * max(0, custom.bodyguard)
    base += [ROLE_LAWYER] * max(0, custom.lawyer)
    base += [ROLE_HOBO] * max(0, custom.hobo)

    base = base[:n]
    base += [ROLE_CIVILIAN] * (n - len(base))
    random.shuffle(base)
    return base

def active_roles_for_game(g: Game, n: int) -> List[str]:
    if g.mode == "classic":
        return build_roles_classic(n)
    if g.mode == "full":
        return build_roles_full(n)
    return build_roles_custom(n, g.custom)

def night_needed_actions(g: Game) -> Tuple[int, int]:
    total = 0
    done = 0

    mafia_alive = g.mafia_alive_ids()
    if mafia_alive:
        total += len(mafia_alive)
        done += sum(1 for uid in mafia_alive if uid in g.mafia_votes)

    def add(uid: Optional[int], chosen: Optional[int], required: bool = True):
        nonlocal total, done
        if not required:
            return
        if g.role_alive(uid):
            total += 1
            if chosen is not None:
                done += 1

    add(g.doctor_id, g.heal_target)
    add(g.sheriff_id, g.sheriff_check)
    add(g.maniac_id, g.maniac_kill, required=(g.mode != "classic"))
    add(g.escort_id, g.escort_block, required=(g.mode != "classic"))
    add(g.bodyguard_id, g.bodyguard_protect, required=(g.mode != "classic"))
    if g.role_alive(g.lawyer_id) and not g.lawyer_used and g.mode != "classic":
        total += 1
        if g.lawyer_protect is not None:
            done += 1
    add(g.hobo_id, g.hobo_watch, required=(g.mode != "classic"))

    return done, total

def night_all_ready(g: Game) -> bool:
    done, total = night_needed_actions(g)
    return total > 0 and done >= total

# =========================
# COMMANDS (PUBLIC)
# =========================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎩 *GAP Мафия — команды*\n\n"
        "В группе:\n"
        "/newgame — создать лобби\n"
        "/settings — настройки\n"
        "/speed fast|normal|slow\n"
        "/settimes night|day|vote <сек>\n"
        "/togglereveal_lynch\n"
        "/togglereveal_night\n"
        "/status\n"
        "/roles\n"
        "/rules\n"
        "/endgame\n\n"
        "Админ:\n"
        "/pause /resume\n"
        "/skipnight /skipday\n"
        "/forcevote\n"
        "/kick @user\n"
        "/setrole @user role\n"
        "/revealall\n\n"
        "В личке:\n"
        "/start — чтобы бот мог писать тебе\n"
        "Роли и ночные действия — в ЛС.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await cmd_help(update, context)
        return
    await update.message.reply_text(
        "✅ Отлично. Теперь я могу отправлять тебе роль и секреты.\n"
        "Игры запускаются в группе командой /newgame."
    )

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    cancel_job(context, g)

    # reset but keep cfg/custom
    cfg = g.cfg
    custom = g.custom
    GAMES[chat_id] = Game(chat_id=chat_id, phase="lobby", mode="classic", cfg=cfg, custom=custom)
    g = GAMES[chat_id]

    msg = await update.message.reply_text(
        "🎭 *Лобби создано!*\n\n"
        "1) Все нажимают 🎭 Присоединиться\n"
        "2) Выберите режим\n"
        "3) Нажмите ▶️ Начать\n\n"
        "⚠️ Каждый игрок должен открыть бота в личке и нажать /start, иначе роль не придёт.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_lobby(chat_id)
    )
    g.lobby_msg_id = msg.message_id

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if g.phase == "idle":
        await update.message.reply_text("Игры нет. /newgame")
        return
    await update.message.reply_text(
        f"📌 *Статус*\n"
        f"Фаза: *{g.phase}*{' (PAUSE)' if g.paused else ''}\n"
        f"Режим: *{g.mode}*\n"
        f"Игроков: *{len(g.players)}*\n"
        f"🟢 Живые: {names_list(g, only_alive=True)}\n"
        f"🔴 Выбыли: {', '.join(p.name for p in g.players.values() if not p.alive) or '—'}",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    n = len(g.players)
    if g.phase == "idle":
        await update.message.reply_text("Игры нет. /newgame")
        return
    if n == 0:
        await update.message.reply_text("Пока нет игроков.")
        return
    roles = active_roles_for_game(g, n)
    counts: Dict[str, int] = {}
    for r in roles:
        counts[r] = counts.get(r, 0) + 1
    lines = [f"🎭 *Роли (режим: {g.mode})* игроков: *{n}*"]
    for r, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"• {role_name(r)} — {c}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 *Правила (кратко)*\n\n"
        "Ночь: роли делают действия в ЛС.\n"
        "День: обсуждение → голосование в группе.\n"
        "Мафия выигрывает, если мафии стало не меньше, чем остальных.\n"
        "Мирные выигрывают, если мафия (и маньяк) устранены.\n"
        "Маньяк выигрывает, если остался один.\n\n"
        "Подсказка: если кто-то не получает роль — пусть нажмёт /start в личке бота.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    await update.message.reply_text(
        "⚙️ *Настройки*\n"
        f"• MIN игроков: *{g.cfg.min_players}*\n"
        f"• Ночь: *{g.cfg.night_seconds} сек*\n"
        f"• День: *{g.cfg.day_seconds} сек*\n"
        f"• Голосование: *{g.cfg.vote_seconds} сек*\n"
        f"• Роль после казни: *{'ДА' if g.cfg.reveal_role_on_lynch else 'НЕТ'}*\n"
        f"• Роль после ночи: *{'ДА' if g.cfg.reveal_role_on_night_death else 'НЕТ'}*\n"
        f"• Ничья днём: *{g.cfg.tie_policy}*\n"
        f"• Доктор самолечение лимит: *{g.cfg.doctor_self_heal_limit}*\n"
        f"• Доктор без повтора цели: *{'ДА' if g.cfg.doctor_no_repeat_target else 'НЕТ'}*\n"
        f"• Путана без повтора цели: *{'ДА' if g.cfg.escort_no_repeat_target else 'НЕТ'}*\n",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Использование: /speed fast|normal|slow")
        return
    p = context.args[0].lower()
    if p == "fast":
        g.cfg.night_seconds, g.cfg.day_seconds, g.cfg.vote_seconds = 45, 60, 45
    elif p == "normal":
        g.cfg.night_seconds, g.cfg.day_seconds, g.cfg.vote_seconds = 60, 90, 60
    elif p == "slow":
        g.cfg.night_seconds, g.cfg.day_seconds, g.cfg.vote_seconds = 90, 150, 90
    else:
        await update.message.reply_text("fast|normal|slow")
        return
    await update.message.reply_text(f"✅ Скорость: {p}")

async def cmd_settimes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /settimes night|day|vote <сек>")
        return
    which = context.args[0].lower()
    try:
        secs = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Секунды должны быть числом.")
        return
    if secs < 10 or secs > 600:
        await update.message.reply_text("Поставь от 10 до 600 секунд.")
        return
    if which == "night":
        g.cfg.night_seconds = secs
    elif which == "day":
        g.cfg.day_seconds = secs
    elif which == "vote":
        g.cfg.vote_seconds = secs
    else:
        await update.message.reply_text("night|day|vote")
        return
    await update.message.reply_text(f"✅ {which} = {secs} сек")

async def cmd_togglereveal_lynch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    g.cfg.reveal_role_on_lynch = not g.cfg.reveal_role_on_lynch
    await update.message.reply_text(f"✅ Роль после казни: {'ВКЛ' if g.cfg.reveal_role_on_lynch else 'ВЫКЛ'}")

async def cmd_togglereveal_night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    g.cfg.reveal_role_on_night_death = not g.cfg.reveal_role_on_night_death
    await update.message.reply_text(f"✅ Роль после ночи: {'ВКЛ' if g.cfg.reveal_role_on_night_death else 'ВЫКЛ'}")

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    chat_id = update.effective_chat.id
    g = get_game(chat_id)
    cancel_job(context, g)
    if chat_id in GAMES:
        del GAMES[chat_id]
    await update.message.reply_text("🛑 Игра сброшена.")

# =========================
# ADMIN COMMANDS
# =========================
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    g.paused = True
    cancel_job(context, g)
    await update.message.reply_text("⏸ Игра на паузе.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    g.paused = False
    await update.message.reply_text("▶️ Игра продолжена. (Если нужно — вручную /skipday или /skipnight)")

async def cmd_skipnight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    await resolve_night(g.chat_id, context, forced=True, reason="⏭ Ночь пропущена админом.")

async def cmd_skipday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if g.phase != "day":
        await update.message.reply_text("Можно пропустить только днём.")
        return
    await start_vote(g.chat_id, context, auto=False, reason="⏭ День пропущен админом → голосование.")

async def cmd_forcevote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if g.phase not in ("day", "voting"):
        await update.message.reply_text("Сейчас нельзя.")
        return
    await start_vote(g.chat_id, context, auto=False, reason="🧰 Форс-голосование админом.")

async def cmd_revealall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    await reveal_roles(g.chat_id, context)

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Использование: /kick <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Нужно user_id числом.")
        return
    if uid in g.players:
        del g.players[uid]
        await update.message.reply_text("✅ Удалён из игры.")
    else:
        await update.message.reply_text("Такого игрока нет.")

async def cmd_setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    g = get_game(update.effective_chat.id)
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /setrole <user_id> <role>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id числом.")
        return
    role = context.args[1].lower()
    if role not in ROLE_RU:
        await update.message.reply_text(f"Роль должна быть одной из: {', '.join(ROLE_RU.keys())}")
        return
    if uid not in g.players:
        await update.message.reply_text("Игрока нет в игре.")
        return
    g.players[uid].role = role
    await update.message.reply_text("✅ Роль поставлена (тестовый режим).")

# =========================
# GAME FLOW
# =========================
async def update_lobby_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    if g.phase != "lobby" or not g.lobby_msg_id:
        return
    txt = (
        "🎭 *Лобби*\n"
        f"Режим: *{g.mode}*\n"
        f"Игроков: *{len(g.players)}* (мин: {g.cfg.min_players})\n"
        f"Список: {names_list(g)}\n\n"
        "Жмите кнопки ниже."
    )
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=g.lobby_msg_id,
            text=txt,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_lobby(chat_id),
        )
    except Exception:
        pass

async def start_night(chat_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str = ""):
    g = get_game(chat_id)
    if g.paused:
        return
    cancel_job(context, g)

    g.phase = "night"
    g.night_num += 1

    # reset
    g.mafia_votes = {}
    g.maniac_kill = None
    g.heal_target = None
    g.sheriff_check = None
    g.escort_block = None
    g.bodyguard_protect = None
    g.lawyer_protect = None
    g.hobo_watch = None
    g.visitors = {}
    g.protected_from_vote = None

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 *НОЧЬ {g.night_num}* началась. {reason}\n"
             f"⏳ Таймер: {g.cfg.night_seconds} сек\n"
             f"✅ Прогресс: 0/0",
        parse_mode=ParseMode.MARKDOWN
    )
    g.night_status_msg_id = msg.message_id
    track(g, f"Ночь {g.night_num} началась")

    # send actions in DM
    mafia_alive = g.mafia_alive_ids()
    if mafia_alive:
        mafia_names = ", ".join(g.players[uid].name for uid in mafia_alive if uid in g.players)
        for uid in mafia_alive:
            ok = await safe_dm(
                context,
                uid,
                f"🔫 *Команда мафии:* {mafia_names}\nВыбери цель:",
                kb_targets(chat_id, "KILL", g, exclude_uid=uid)
            )
            if not ok:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ {g.players[uid].name} не открыл(а) бота в личке (/start).")

    if g.role_alive(g.maniac_id):
        await safe_dm(context, g.maniac_id, "🩸 *Маньяк:* выбери жертву.", kb_targets(chat_id, "MKILL", g, exclude_uid=g.maniac_id))
    if g.role_alive(g.doctor_id):
        await safe_dm(context, g.doctor_id, "💉 *Доктор:* кого лечить?", kb_targets(chat_id, "HEAL", g))
    if g.role_alive(g.sheriff_id):
        await safe_dm(context, g.sheriff_id, "🕵️ *Комиссар:* кого проверить?", kb_targets(chat_id, "CHECK", g, exclude_uid=g.sheriff_id))
    if g.role_alive(g.escort_id):
        await safe_dm(context, g.escort_id, "💃 *Путана:* кого блокировать?", kb_targets(chat_id, "BLOCK", g, exclude_uid=g.escort_id))
    if g.role_alive(g.bodyguard_id):
        await safe_dm(context, g.bodyguard_id, "🛡️ *Телохранитель:* кого защищать?", kb_targets(chat_id, "GUARD", g, exclude_uid=g.bodyguard_id))
    if g.role_alive(g.lawyer_id):
        if g.lawyer_used:
            await safe_dm(context, g.lawyer_id, "⚖️ *Адвокат:* способность уже использована.")
        else:
            await safe_dm(context, g.lawyer_id, "⚖️ *Адвокат:* кого защитить от казни (1 раз)?", kb_targets(chat_id, "LAW", g))
    if g.role_alive(g.hobo_id):
        await safe_dm(context, g.hobo_id, "🧥 *Бомж:* за кем следить?", kb_targets(chat_id, "WATCH", g, exclude_uid=g.hobo_id))

    await update_night_progress(chat_id, context)
    schedule_job(context, g, g.cfg.night_seconds, night_timeout, "night_timeout")

async def update_night_progress(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    if g.phase != "night" or not g.night_status_msg_id:
        return
    done, total = night_needed_actions(g)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=g.night_status_msg_id,
            text=f"🌙 *НОЧЬ {g.night_num}* идёт.\n"
                 f"⏳ Таймер: {g.cfg.night_seconds} сек\n"
                 f"✅ Прогресс: *{done}/{total}*",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass

async def night_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_night(chat_id, context, forced=True, reason="⏰ Ночь закончилась (таймер).")

async def resolve_night(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced: bool, reason: str):
    g = get_game(chat_id)
    if g.phase != "night" or g.paused:
        return
    cancel_job(context, g)

    await context.bot.send_message(chat_id=chat_id, text=reason)
    track(g, f"Ночь {g.night_num} завершена")

    mafia_kill = mafia_kill_target(g)
    maniac_kill = g.maniac_kill
    heal = g.heal_target
    guard = g.bodyguard_protect
    law = g.lawyer_protect
    watch = g.hobo_watch
    check = g.sheriff_check

    # apply blocks
    if is_blocked(g, g.maniac_id): maniac_kill = None
    if is_blocked(g, g.doctor_id): heal = None
    if is_blocked(g, g.bodyguard_id): guard = None
    if is_blocked(g, g.lawyer_id): law = None
    if is_blocked(g, g.hobo_id): watch = None
    if is_blocked(g, g.sheriff_id): check = None

    # visitors tracking
    if mafia_kill:
        for uid in g.mafia_alive_ids():
            if uid in g.mafia_votes:
                track_visit(g, uid, mafia_kill)
    if maniac_kill:
        track_visit(g, g.maniac_id, maniac_kill)
    if heal:
        track_visit(g, g.doctor_id, heal)
    if guard:
        track_visit(g, g.bodyguard_id, guard)
    if law and not g.lawyer_used:
        track_visit(g, g.lawyer_id, law)
    if watch:
        track_visit(g, g.hobo_id, watch)
    if check:
        track_visit(g, g.sheriff_id, check)

    # lawyer day protection
    if law and not g.lawyer_used and law in g.players and g.players[law].alive:
        g.lawyer_used = True
        g.protected_from_vote = law
        await safe_dm(context, g.lawyer_id, f"⚖️ Защита от казни: *{g.players[law].name}*")

    deaths: Set[int] = set()

    def apply_attack(target: Optional[int]) -> Optional[int]:
        if not target or target not in g.players or not g.players[target].alive:
            return None
        if guard and target == guard and g.role_alive(g.bodyguard_id):
            deaths.add(g.bodyguard_id)
            return None
        return target

    mafia_target = apply_attack(mafia_kill)
    maniac_target = apply_attack(maniac_kill)

    # doctor cancels + restriction memory
    if heal and heal in g.players:
        g.doctor_last_target = heal

    # escort memory
    if g.escort_block and g.escort_block in g.players:
        g.escort_last_target = g.escort_block

    if mafia_target and (not heal or heal != mafia_target):
        deaths.add(mafia_target)
    if maniac_target and (not heal or heal != maniac_target):
        deaths.add(maniac_target)

    killed: List[Tuple[str, Optional[str]]] = []
    for uid in list(deaths):
        if uid in g.players and g.players[uid].alive:
            g.players[uid].alive = False
            killed.append((g.players[uid].name, g.players[uid].role))

    # sheriff result (Don appears NOT mafia)
    if check and g.role_alive(g.sheriff_id) and check in g.players:
        is_mafia = (check in g.mafia_ids) and (check != g.don_id)
        await safe_dm(context, g.sheriff_id, f"🕵️ Проверка: *{g.players[check].name}* — {'МАФИЯ' if is_mafia else 'НЕ мафия'}")

    # hobo
    if watch and g.role_alive(g.hobo_id) and watch in g.players:
        visitors = {uid for uid in g.visitors.get(watch, set()) if uid != g.hobo_id}
        if not visitors:
            msg = f"🧥 К *{g.players[watch].name}* никто не приходил."
        else:
            msg = f"🧥 К *{g.players[watch].name}* приходили: " + ", ".join(g.players[uid].name for uid in visitors if uid in g.players)
        await safe_dm(context, g.hobo_id, msg)

    # morning message
    if not killed:
        await context.bot.send_message(chat_id=chat_id, text="🌅 Утро. Ночью никто не погиб.")
        track(g, "Ночью никто не погиб")
    else:
        if g.cfg.reveal_role_on_night_death:
            txt = "🌅 Утро. Ночью погибли:\n" + "\n".join([f"• *{n}* ({role_name(r)})" for n, r in killed])
        else:
            txt = "🌅 Утро. Ночью погибли: " + ", ".join([f"*{n}*" for n, _ in killed])
        await context.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.MARKDOWN)
        track(g, "Ночью погибли: " + ", ".join(n for n, _ in killed))

    win = g.check_win()
    if win:
        g.phase = "ended"
        await context.bot.send_message(chat_id=chat_id, text=win, parse_mode=ParseMode.MARKDOWN)
        await reveal_roles(chat_id, context)
        await show_history(chat_id, context)
        return

    # start day discussion
    g.phase = "day"
    g.day_num += 1
    g.day_votes = {}
    extra = ""
    if g.protected_from_vote and g.players.get(g.protected_from_vote) and g.players[g.protected_from_vote].alive:
        extra = "⚖️ Сегодня один игрок защищён от казни (кто — не раскрывается).\n"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"☀️ *ДЕНЬ {g.day_num}*\n{extra}Обсуждение: {g.cfg.day_seconds} сек.",
        parse_mode=ParseMode.MARKDOWN
    )
    track(g, f"День {g.day_num} начался")
    schedule_job(context, g, g.cfg.day_seconds, day_timeout, "day_timeout")

async def day_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    g = get_game(chat_id)
    await start_vote(chat_id, context, auto=True, reason="🤖 Авто-голосование (таймер дня).")

async def start_vote(chat_id: int, context: ContextTypes.DEFAULT_TYPE, auto: bool, reason: str):
    g = get_game(chat_id)
    if g.paused:
        return
    if g.phase not in ("day", "voting"):
        return
    cancel_job(context, g)
    g.phase = "voting"
    g.day_votes = {}

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🗳️ *ГОЛОСОВАНИЕ* ({g.cfg.vote_seconds} сек)\n{reason}\nВыберите, кого казнить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_vote(chat_id, g)
    )
    schedule_job(context, g, g.cfg.vote_seconds, vote_timeout, "vote_timeout")

async def vote_timeout(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await resolve_vote(chat_id, context, forced=True, reason="⏰ Голосование завершено (таймер).")

def vote_result(g: Game) -> Optional[int]:
    votes = [t for voter, t in g.day_votes.items() if voter in g.players and g.players[voter].alive and t in g.players and g.players[t].alive]
    if not votes:
        return None
    counts: Dict[int, int] = {}
    for t in votes:
        counts[t] = counts.get(t, 0) + 1
    mx = max(counts.values())
    top = [t for t, c in counts.items() if c == mx]
    if len(top) == 1:
        return top[0]

    # tie
    if g.cfg.tie_policy == "nobody":
        return -1
    if g.cfg.tie_policy == "revote":
        return -2
    return random.choice(top)

async def resolve_vote(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced: bool, reason: str):
    g = get_game(chat_id)
    if g.phase != "voting" or g.paused:
        return
    cancel_job(context, g)

    await context.bot.send_message(chat_id=chat_id, text=reason)
    track(g, f"Голосование завершено: {reason}")

    target = vote_result(g)
    if target is None:
        await context.bot.send_message(chat_id=chat_id, text="Никого не выбрали. Начинается ночь.")
        track(g, "Днём никого не выбрали")
        await start_night(chat_id, context)
        return
    if target == -1:
        await context.bot.send_message(chat_id=chat_id, text="⚖️ Ничья. Никого не казнили. Начинается ночь.")
        track(g, "Ничья — никто не казнён")
        await start_night(chat_id, context)
        return
    if target == -2:
        await context.bot.send_message(chat_id=chat_id, text="⚖️ Ничья. Повторное голосование.")
        track(g, "Ничья — повтор голосования")
        await start_vote(chat_id, context, auto=False, reason="Повторное голосование из-за ничьей.")
        return

    if target not in g.players or not g.players[target].alive:
        await context.bot.send_message(chat_id=chat_id, text="Цель недоступна. Начинается ночь.")
        await start_night(chat_id, context)
        return

    if g.protected_from_vote == target and g.players[target].alive:
        await context.bot.send_message(chat_id=chat_id, text="⚖️ Казнь отменена: игрок был защищён сегодня.")
        track(g, "Казнь отменена адвокатом")
        await start_night(chat_id, context)
        return

    g.players[target].alive = False
    if g.cfg.reveal_role_on_lynch:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 Казнён(а): *{g.players[target].name}* — *{role_name(g.players[target].role)}*",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"🚫 Казнён(а): *{g.players[target].name}*.", parse_mode=ParseMode.MARKDOWN)
    track(g, f"Казнён: {g.players[target].name}")

    win = g.check_win()
    if win:
        g.phase = "ended"
        await context.bot.send_message(chat_id=chat_id, text=win, parse_mode=ParseMode.MARKDOWN)
        await reveal_roles(chat_id, context)
        await show_history(chat_id, context)
        return

    await start_night(chat_id, context)

async def reveal_roles(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    lines = ["🎭 *Роли игроков:*"]
    for p in g.players.values():
        status = "🟢" if p.alive else "🔴"
        lines.append(f"{status} {p.name} — *{role_name(p.role)}*")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def show_history(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = get_game(chat_id)
    if not g.log_lines:
        return
    # keep it short
    tail = g.log_lines[-25:]
    await context.bot.send_message(chat_id=chat_id, text="🧾 *История (последнее):*\n" + "\n".join("• " + x for x in tail), parse_mode=ParseMode.MARKDOWN)

# =========================
# CALLBACKS
# =========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()

    data = q.data
    parts = data.split(":")
    if len(parts) < 2:
        return

    # lobby actions
    if parts[0].startswith("LOBBY_"):
        kind = parts[0]
        chat_id = int(parts[1])
        g = get_game(chat_id)
        user = q.from_user

        if kind == "LOBBY_JOIN":
            if g.phase != "lobby":
                await q.answer("Лобби закрыто.", show_alert=True)
                return
            if user.id in g.players:
                await q.answer("Ты уже в игре.", show_alert=True)
                return
            g.players[user.id] = Player(user_id=user.id, name=user.full_name)
            await context.bot.send_message(chat_id=chat_id, text=f"✅ *{user.full_name}* в игре.", parse_mode=ParseMode.MARKDOWN)
            await update_lobby_message(chat_id, context)
            await q.answer("Добавлено. Нажми /start в личке бота.", show_alert=True)
            return

        if kind == "LOBBY_LEAVE":
            if g.phase != "lobby":
                await q.answer("Во время игры нельзя.", show_alert=True)
                return
            if user.id not in g.players:
                await q.answer("Тебя нет в лобби.", show_alert=True)
                return
            del g.players[user.id]
            await context.bot.send_message(chat_id=chat_id, text=f"🚪 *{user.full_name}* вышел(ла).", parse_mode=ParseMode.MARKDOWN)
            await update_lobby_message(chat_id, context)
            return

        if kind == "LOBBY_LIST":
            await q.answer(f"Игроки: {names_list(g)}", show_alert=True)
            return

        if kind == "LOBBY_STOP":
            cancel_job(context, g)
            if chat_id in GAMES:
                del GAMES[chat_id]
            await context.bot.send_message(chat_id=chat_id, text="🛑 Игра сброшена.")
            return

        if kind == "LOBBY_MODE":
            # format: LOBBY_MODE:mode:chat
            if len(parts) != 3:
                return
            mode = parts[1]
            chat_id = int(parts[2])
            g = get_game(chat_id)
            if g.phase != "lobby":
                await q.answer("Режим можно менять только в лобби.", show_alert=True)
                return
            g.mode = mode if mode in ("classic", "full", "custom") else "classic"
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Режим: *{g.mode}*", parse_mode=ParseMode.MARKDOWN)
            await update_lobby_message(chat_id, context)
            return

        if kind == "LOBBY_BEGIN":
            if g.phase != "lobby":
                await q.answer("Игра уже идёт.", show_alert=True)
                return
            if len(g.players) < g.cfg.min_players:
                await q.answer(f"Нужно минимум {g.cfg.min_players}.", show_alert=True)
                return

            ids = list(g.players.keys())
            random.shuffle(ids)
            roles = active_roles_for_game(g, len(ids))

            # reset role holders
            g.mafia_ids = set()
            g.don_id = g.doctor_id = g.sheriff_id = g.maniac_id = g.escort_id = g.bodyguard_id = g.lawyer_id = g.hobo_id = None
            g.lawyer_used = False
            g.protected_from_vote = None
            g.doctor_self_heals_used = 0
            g.doctor_last_target = None
            g.escort_last_target = None

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

            g.day_num = 0
            g.night_num = 0
            g.log_lines = []

            await context.bot.send_message(chat_id=chat_id, text=f"🎬 *Игра началась!* Режим: *{g.mode}*\nРоли — в личку.", parse_mode=ParseMode.MARKDOWN)

            failed = []
            for uid in ids:
                ok = await safe_dm(context, uid, f"🎭 Твоя роль: *{role_name(g.players[uid].role)}*\nНе показывай никому.\n"
                                                 f"Ночью выбирай действия кнопками.", None)
                if not ok:
                    failed.append(g.players[uid].name)

            if failed:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Не смог отправить роли (они не нажали /start в личке):\n" + "\n".join("• " + n for n in failed)
                )

            await start_night(chat_id, context)
            return

        return

    # other actions: ACTION:chat_id:target
    if len(parts) != 3:
        return

    action, chat_s, target_s = parts
    chat_id = int(chat_s)
    target_uid = int(target_s)

    g = get_game(chat_id)

    if action == "STATUSBTN":
        await q.answer(f"Живые: {names_list(g, only_alive=True)}", show_alert=True)
        return

    actor_uid = q.from_user.id
    if actor_uid not in g.players:
        await q.answer("Ты не игрок.", show_alert=True)
        return
    if not g.players[actor_uid].alive:
        await q.answer("Ты выбыл(а).", show_alert=True)
        return

    # voting
    if action == "VOTE":
        if g.phase != "voting" or g.paused:
            await q.answer("Сейчас нет голосования.", show_alert=True)
            return
        if target_uid == 0:
            if actor_uid in g.day_votes:
                del g.day_votes[actor_uid]
            await q.answer("Голос снят.")
            return
        if target_uid == actor_uid:
            await q.answer("Нельзя за себя.", show_alert=True)
            return
        if target_uid not in g.players or not g.players[target_uid].alive:
            await q.answer("Цель недоступна.", show_alert=True)
            return

        g.day_votes[actor_uid] = target_uid
        await q.answer("Голос принят.")

        # early majority
        alive_count = len(g.alive_players())
        needed = (alive_count // 2) + 1
        counts: Dict[int, int] = {}
        for voter, tgt in g.day_votes.items():
            if voter in g.players and g.players[voter].alive:
                counts[tgt] = counts.get(tgt, 0) + 1
        for tgt, c in counts.items():
            if c >= needed:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Большинство ({c}/{alive_count}). Завершаю раньше таймера.")
                await resolve_vote(chat_id, context, forced=False, reason="✅ Завершено по большинству.")
                return
        return

    # night actions only in night
    if g.phase != "night" or g.paused:
        await q.answer("Сейчас не ночь.", show_alert=True)
        return

    # allow skip = target_uid 0
    if target_uid != 0:
        if target_uid not in g.players or not g.players[target_uid].alive:
            await q.answer("Цель недоступна.", show_alert=True)
            return
        if target_uid == actor_uid and action in ("KILL", "MKILL", "CHECK", "BLOCK", "GUARD", "WATCH"):
            await q.answer("Нельзя выбрать себя.", show_alert=True)
            return

    # mafia kill vote
    if action == "KILL":
        if actor_uid not in g.mafia_ids:
            await q.answer("Ты не мафия/дон.", show_alert=True)
            return
        if target_uid == 0:
            if actor_uid in g.mafia_votes:
                del g.mafia_votes[actor_uid]
            await safe_dm(context, actor_uid, "⏭ Ты пропустил голос.")
        else:
            g.mafia_votes[actor_uid] = target_uid
            await safe_dm(context, actor_uid, f"🔫 Твой голос: *{g.players[target_uid].name}*")

    elif action == "MKILL":
        if g.maniac_id != actor_uid:
            await q.answer("Ты не Маньяк.", show_alert=True)
            return
        g.maniac_kill = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"🩸 Жертва: *{g.players[target_uid].name}*")

    elif action == "HEAL":
        if g.doctor_id != actor_uid:
            await q.answer("Ты не Доктор.", show_alert=True)
            return
        if target_uid == actor_uid:
            # self-heal limit
            if g.doctor_self_heals_used >= g.cfg.doctor_self_heal_limit:
                await q.answer("Самолечение больше нельзя.", show_alert=True)
                return
            g.doctor_self_heals_used += 1

        if target_uid != 0 and g.cfg.doctor_no_repeat_target and g.doctor_last_target == target_uid:
            await q.answer("Нельзя лечить одного и того же 2 ночи подряд.", show_alert=True)
            return

        g.heal_target = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"💉 Лечишь: *{g.players[target_uid].name}*")

    elif action == "CHECK":
        if g.sheriff_id != actor_uid:
            await q.answer("Ты не Комиссар.", show_alert=True)
            return
        g.sheriff_check = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"🕵️ Проверяешь: *{g.players[target_uid].name}*")

    elif action == "BLOCK":
        if g.escort_id != actor_uid:
            await q.answer("Ты не Путана.", show_alert=True)
            return
        if target_uid != 0 and g.cfg.escort_no_repeat_target and g.escort_last_target == target_uid:
            await q.answer("Нельзя блокировать одного и того же 2 ночи подряд.", show_alert=True)
            return
        g.escort_block = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"💃 Блок: *{g.players[target_uid].name}*")

    elif action == "GUARD":
        if g.bodyguard_id != actor_uid:
            await q.answer("Ты не Телохранитель.", show_alert=True)
            return
        g.bodyguard_protect = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"🛡️ Защита: *{g.players[target_uid].name}*")

    elif action == "LAW":
        if g.lawyer_id != actor_uid:
            await q.answer("Ты не Адвокат.", show_alert=True)
            return
        if g.lawyer_used:
            await q.answer("Уже использовано.", show_alert=True)
            return
        g.lawyer_protect = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"⚖️ Защитишь: *{g.players[target_uid].name}*")

    elif action == "WATCH":
        if g.hobo_id != actor_uid:
            await q.answer("Ты не Бомж.", show_alert=True)
            return
        g.hobo_watch = None if target_uid == 0 else target_uid
        await safe_dm(context, actor_uid, "⏭ Пропуск." if target_uid == 0 else f"🧥 Следишь за: *{g.players[target_uid].name}*")

    await update_night_progress(chat_id, context)

    # early end
    if night_all_ready(g):
        await resolve_night(chat_id, context, forced=False, reason="✅ Все сделали выбор. Ночь завершаю раньше таймера.")

# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    # public
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))

    # game
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("speed", cmd_speed))
    app.add_handler(CommandHandler("settimes", cmd_settimes))
    app.add_handler(CommandHandler("togglereveal_lynch", cmd_togglereveal_lynch))
    app.add_handler(CommandHandler("togglereveal_night", cmd_togglereveal_night))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    # admin
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("skipnight", cmd_skipnight))
    app.add_handler(CommandHandler("skipday", cmd_skipday))
    app.add_handler(CommandHandler("forcevote", cmd_forcevote))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("setrole", cmd_setrole))
    app.add_handler(CommandHandler("revealall", cmd_revealall))

    # callbacks
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("GAP Mafia bot running (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()
