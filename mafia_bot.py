import os
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, List

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================
# TOKEN
# =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable is not set")


# =========================
# ИГРОВЫЕ СТРУКТУРЫ
# =========================
@dataclass
class Player:
    user_id: int
    name: str
    alive: bool = True
    role: Optional[str] = None  # "mafia", "detective", "doctor", "citizen"


@dataclass
class GameState:
    chat_id: int
    host_id: int
    phase: str = "lobby"  # "lobby" | "night" | "day" | "ended"
    players: Dict[int, Player] = field(default_factory=dict)

    mafia_ids: Set[int] = field(default_factory=set)
    detective_id: Optional[int] = None
    doctor_id: Optional[int] = None

    # Ночь
    mafia_target: Optional[int] = None
    detective_check: Optional[int] = None
    doctor_save: Optional[int] = None
    night_done: Set[int] = field(default_factory=set)  # кто уже сделал действие ночью

    # День
    votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id
    day_round: int = 0


GAMES: Dict[int, GameState] = {}  # chat_id -> GameState


# =========================
# ВСПОМОГАТЕЛЬНОЕ
# =========================
def mention_name(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "Игрок"
    return (u.full_name or u.username or str(u.id)).strip()


def get_game(chat_id: int) -> Optional[GameState]:
    return GAMES.get(chat_id)


def alive_players(game: GameState) -> List[Player]:
    return [p for p in game.players.values() if p.alive]


def alive_ids(game: GameState) -> Set[int]:
    return {p.user_id for p in game.players.values() if p.alive}


def role_name_ru(role: str) -> str:
    return {
        "mafia": "🕶️ Мафия",
        "detective": "🕵️ Детектив",
        "doctor": "🩺 Доктор",
        "citizen": "🙂 Мирный",
    }.get(role, role)


def is_group_chat(update: Update) -> bool:
    ct = update.effective_chat.type if update.effective_chat else None
    return ct in (ChatType.GROUP, ChatType.SUPERGROUP)


def ensure_group(update: Update) -> bool:
    return is_group_chat(update)


def ensure_private(update: Update) -> bool:
    return (update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


def format_players_list(game: GameState, show_roles: bool = False) -> str:
    lines = []
    for p in game.players.values():
        status = "✅" if p.alive else "☠️"
        if show_roles and p.role:
            lines.append(f"{status} {p.name} — {role_name_ru(p.role)}")
        else:
            lines.append(f"{status} {p.name}")
    return "\n".join(lines) if lines else "Пока нет игроков."


def count_alive_roles(game: GameState):
    mafia = 0
    others = 0
    for p in game.players.values():
        if not p.alive:
            continue
        if p.role == "mafia":
            mafia += 1
        else:
            others += 1
    return mafia, others


def check_win(game: GameState) -> Optional[str]:
    mafia, others = count_alive_roles(game)
    if mafia <= 0 and game.phase != "ended":
        return "🎉 Мирные победили! Мафия устранена."
    if mafia >= others and game.phase != "ended":
        return "💀 Мафия победила! У мафии контроль."
    return None


async def safe_dm(app: Application, user_id: int, text: str):
    try:
        await app.bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception:
        return False


def pick_roles(n: int):
    """
    Очень простая балансировка:
    4-5 игроков: 1 мафия, 1 детектив, 1 доктор
    6-8 игроков: 2 мафии, 1 детектив, 1 доктор
    9-11: 3 мафии, 1 детектив, 1 доктор
    """
    if n < 4:
        return None
    mafia = 1
    if n >= 6:
        mafia = 2
    if n >= 9:
        mafia = 3
    roles = ["mafia"] * mafia + ["detective", "doctor"]
    while len(roles) < n:
        roles.append("citizen")
    random.shuffle(roles)
    return roles


# =========================
# КОМАНДЫ: HELP / START
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ensure_private(update):
        await update.message.reply_text(
            "🎩 GAP Мафия\n\n"
            "Лучше играть в групповом чате.\n"
            "Добавь бота в группу и используй /newgame.\n\n"
            "Команды в группе:\n"
            "/newgame — создать игру\n"
            "/join — войти\n"
            "/leave — выйти\n"
            "/startgame — начать\n"
            "/status — статус\n"
            "/vote <имя/часть имени> — голос (днём)\n"
            "/endgame — закончить\n\n"
            "Команды в ЛС (ночью):\n"
            "/kill <имя> — мафия\n"
            "/check <имя> — детектив\n"
            "/protect <имя> — доктор"
        )
        return

    await update.message.reply_text(
        "🎩 GAP Мафия в чате!\n"
        "Сначала создай игру: /newgame"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# =========================
# ГРУППА: СОЗДАТЬ / ВОЙТИ / ВЫЙТИ / СТАТУС
# =========================
async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Создай игру в групповом чате 🙂")
        return

    chat_id = update.effective_chat.id
    host_id = update.effective_user.id

    if chat_id in GAMES and GAMES[chat_id].phase != "ended":
        await update.message.reply_text("Игра уже есть. Используй /endgame чтобы завершить.")
        return

    GAMES[chat_id] = GameState(chat_id=chat_id, host_id=host_id)
    await update.message.reply_text(
        "🃏 Новая игра создана!\n\n"
        "Игроки, пишите /join\n"
        "Хост начинает: /startgame\n\n"
        "Минимум 4 игрока."
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Войти можно только в группе.")
        return

    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase == "ended":
        await update.message.reply_text("Нет активной игры. Создай: /newgame")
        return
    if game.phase != "lobby":
        await update.message.reply_text("Нельзя присоединиться — игра уже началась.")
        return

    uid = update.effective_user.id
    name = mention_name(update)

    if uid in game.players:
        await update.message.reply_text("Ты уже в игре ✅")
        return

    game.players[uid] = Player(user_id=uid, name=name)
    await update.message.reply_text(
        f"✅ {name} вошёл(ла) в игру.\n\n"
        f"Сейчас игроков: {len(game.players)}\n"
        f"Список:\n{format_players_list(game)}"
    )


async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Выйти можно только в группе.")
        return

    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase == "ended":
        await update.message.reply_text("Нет активной игры.")
        return

    uid = update.effective_user.id
    if uid not in game.players:
        await update.message.reply_text("Тебя нет в игре.")
        return

    if game.phase != "lobby":
        await update.message.reply_text("Нельзя выйти — игра уже началась.")
        return

    name = game.players[uid].name
    del game.players[uid]
    await update.message.reply_text(
        f"↩️ {name} вышел(ла).\n"
        f"Сейчас игроков: {len(game.players)}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase == "ended":
        await update.message.reply_text("Нет активной игры. /newgame")
        return

    await update.message.reply_text(
        f"📌 Фаза: {game.phase}\n"
        f"Игроки:\n{format_players_list(game)}"
    )


# =========================
# СТАРТ ИГРЫ
# =========================
async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Начать игру можно только в группе.")
        return

    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase == "ended":
        await update.message.reply_text("Нет игры. /newgame")
        return

    if update.effective_user.id != game.host_id:
        await update.message.reply_text("Только создатель игры (хост) может начать.")
        return

    if game.phase != "lobby":
        await update.message.reply_text("Игра уже идёт.")
        return

    n = len(game.players)
    roles = pick_roles(n)
    if not roles:
        await update.message.reply_text("Нужно минимум 4 игрока.")
        return

    ids = list(game.players.keys())
    random.shuffle(ids)
    for uid, role in zip(ids, roles):
        game.players[uid].role = role
        if role == "mafia":
            game.mafia_ids.add(uid)
        elif role == "detective":
            game.detective_id = uid
        elif role == "doctor":
            game.doctor_id = uid

    # Разошлём роли в ЛС
    failed = []
    for uid, p in game.players.items():
        ok = await safe_dm(
            context.application,
            uid,
            f"🎭 Твоя роль: {role_name_ru(p.role)}\n\n"
            "Если ЛС закрыты — открой чат с ботом и нажми /start."
        )
        if not ok:
            failed.append(p.name)

    # Старт ночи
    game.phase = "night"
    game.night_done.clear()
    game.mafia_target = None
    game.detective_check = None
    game.doctor_save = None
    game.votes.clear()
    game.day_round = 1

    msg = (
        "🌙 Игра началась! Наступает НОЧЬ.\n\n"
        "Мафия, детектив и доктор — проверьте ЛС и сделайте действие:\n"
        "🕶️ мафия: /kill <имя>\n"
        "🕵️ детектив: /check <имя>\n"
        "🩺 доктор: /protect <имя>\n\n"
        "Остальные ждут утра."
    )
    if failed:
        msg += "\n\n⚠️ Не смог отправить роли в ЛС этим игрокам:\n- " + "\n- ".join(failed) + "\n\nПусть они откроют чат с ботом и нажмут /start, затем начните заново /endgame и /newgame."

    await update.message.reply_text(msg)


# =========================
# ПОИСК ИГРОКА ПО ИМЕНИ
# =========================
def find_player_by_text(game: GameState, text: str, only_alive: bool = True) -> Optional[Player]:
    if not text:
        return None
    t = text.strip().lower()
    candidates = []
    for p in game.players.values():
        if only_alive and not p.alive:
            continue
        if t in p.name.lower():
            candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    return None


# =========================
# НОЧНЫЕ ДЕЙСТВИЯ (ЛС)
# =========================
async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_private(update):
        await update.message.reply_text("Эта команда работает только в ЛС с ботом.")
        return

    uid = update.effective_user.id

    # Найдём игру, где этот игрок мафия и идёт ночь
    game = next((g for g in GAMES.values() if uid in g.players and g.phase == "night"), None)
    if not game:
        await update.message.reply_text("Сейчас нет активной ночи для тебя.")
        return

    if uid not in game.mafia_ids or not game.players[uid].alive:
        await update.message.reply_text("Ты не мафия (или уже выбыл).")
        return

    if not context.args:
        await update.message.reply_text("Используй: /kill <имя>")
        return

    target_text = " ".join(context.args)
    target = find_player_by_text(game, target_text, only_alive=True)
    if not target:
        await update.message.reply_text(
            "Не нашёл однозначно игрока. Пиши уникальную часть имени.\n"
            "Живые игроки:\n" + format_players_list(game)
        )
        return
    if target.user_id == uid:
        await update.message.reply_text("Нельзя выбрать себя.")
        return

    game.mafia_target = target.user_id
    game.night_done.add(uid)

    await update.message.reply_text(f"🕶️ Ок. Вы выбрали цель: {target.name}")
    await try_resolve_night(context.application, game)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_private(update):
        await update.message.reply_text("Эта команда работает только в ЛС с ботом.")
        return

    uid = update.effective_user.id
    game = next((g for g in GAMES.values() if uid in g.players and g.phase == "night"), None)
    if not game:
        await update.message.reply_text("Сейчас нет активной ночи для тебя.")
        return

    if uid != game.detective_id or not game.players[uid].alive:
        await update.message.reply_text("Ты не детектив (или уже выбыл).")
        return

    if not context.args:
        await update.message.reply_text("Используй: /check <имя>")
        return

    target_text = " ".join(context.args)
    target = find_player_by_text(game, target_text, only_alive=True)
    if not target:
        await update.message.reply_text(
            "Не нашёл однозначно игрока. Пиши уникальную часть имени.\n"
            "Живые игроки:\n" + format_players_list(game)
        )
        return

    game.detective_check = target.user_id
    game.night_done.add(uid)

    is_mafia = (target.user_id in game.mafia_ids)
    await update.message.reply_text(
        f"🕵️ Проверка: {target.name} — {'🕶️ МАФИЯ' if is_mafia else '🙂 НЕ мафия'}"
    )
    await try_resolve_night(context.application, game)


async def protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_private(update):
        await update.message.reply_text("Эта команда работает только в ЛС с ботом.")
        return

    uid = update.effective_user.id
    game = next((g for g in GAMES.values() if uid in g.players and g.phase == "night"), None)
    if not game:
        await update.message.reply_text("Сейчас нет активной ночи для тебя.")
        return

    if uid != game.doctor_id or not game.players[uid].alive:
        await update.message.reply_text("Ты не доктор (или уже выбыл).")
        return

    if not context.args:
        await update.message.reply_text("Используй: /protect <имя>")
        return

    target_text = " ".join(context.args)
    target = find_player_by_text(game, target_text, only_alive=True)
    if not target:
        await update.message.reply_text(
            "Не нашёл однозначно игрока. Пиши уникальную часть имени.\n"
            "Живые игроки:\n" + format_players_list(game)
        )
        return

    game.doctor_save = target.user_id
    game.night_done.add(uid)

    await update.message.reply_text(f"🩺 Защита выбрана: {target.name}")
    await try_resolve_night(context.application, game)


async def try_resolve_night(app: Application, game: GameState):
    # Ночь заканчивается, когда:
    # - мафия выбрала цель (хотя бы один мафиози нажал /kill)
    # - детектив сделал check (если жив)
    # - доктор сделал protect (если жив)
    required = set()

    # мафия (любая живая мафия может сделать действие; достаточно одной цели)
    if any(game.players[mid].alive for mid in game.mafia_ids):
        required.add("mafia")

    if game.detective_id and game.players.get(game.detective_id) and game.players[game.detective_id].alive:
        required.add("detective")

    if game.doctor_id and game.players.get(game.doctor_id) and game.players[game.doctor_id].alive:
        required.add("doctor")

    done = set()
    if game.mafia_target is not None:
        done.add("mafia")
    if game.detective_check is not None:
        done.add("detective")
    if game.doctor_save is not None:
        done.add("doctor")

    if not required.issubset(done):
        return

    # Резолв ночи
    killed_player: Optional[Player] = None
    if game.mafia_target is not None:
        target_id = game.mafia_target
        if target_id != game.doctor_save:
            # цель не спасли
            if target_id in game.players and game.players[target_id].alive:
                game.players[target_id].alive = False
                killed_player = game.players[target_id]

    game.phase = "day"
    game.votes.clear()

    # Сброс ночных действий
    game.mafia_target = None
    game.detective_check = None
    game.doctor_save = None
    game.night_done.clear()

    # Сообщение в группу
    if killed_player:
        text = f"☀️ Утро! Ночью был убит: ☠️ {killed_player.name}\n\n"
    else:
        text = "☀️ Утро! Ночью никто не погиб.\n\n"

    win = check_win(game)
    if win:
        game.phase = "ended"
        # покажем роли в финале
        text += win + "\n\n" + "🎭 Роли:\n" + format_players_list(game, show_roles=True)
        await app.bot.send_message(chat_id=game.chat_id, text=text)
        return

    text += (
        "🗳️ День: обсуждение и голосование.\n"
        "Голосовать: /vote <имя>\n"
        "Посмотреть живых: /status\n"
        "Чтобы закончить игру: /endgame"
    )
    await app.bot.send_message(chat_id=game.chat_id, text=text)


# =========================
# ДЕНЬ: ГОЛОСОВАНИЕ В ГРУППЕ
# =========================
async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Голосование только в группе.")
        return

    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game or game.phase == "ended":
        await update.message.reply_text("Нет активной игры.")
        return
    if game.phase != "day":
        await update.message.reply_text("Сейчас не день.")
        return

    voter_id = update.effective_user.id
    if voter_id not in game.players or not game.players[voter_id].alive:
        await update.message.reply_text("Ты не участвуешь (или уже выбыл).")
        return

    if not context.args:
        await update.message.reply_text("Используй: /vote <имя>")
        return

    target_text = " ".join(context.args)
    target = find_player_by_text(game, target_text, only_alive=True)
    if not target:
        await update.message.reply_text(
            "Не нашёл однозначно игрока. Пиши уникальную часть имени.\n"
            "Живые игроки:\n" + format_players_list(game)
        )
        return

    if target.user_id == voter_id:
        await update.message.reply_text("Нельзя голосовать за себя.")
        return

    game.votes[voter_id] = target.user_id
    await update.message.reply_text(f"🗳️ Голос принят: {game.players[voter_id].name} → {target.name}")

    await try_resolve_day(context.application, game)


async def try_resolve_day(app: Application, game: GameState):
    alive = alive_ids(game)
    if not alive:
        return

    # если проголосовали все живые — завершаем день
    if not alive.issubset(set(game.votes.keys())):
        return

    # подсчёт
    tally: Dict[int, int] = {}
    for voter, target in game.votes.items():
        if voter in alive and target in alive:
            tally[target] = tally.get(target, 0) + 1

    if not tally:
        await app.bot.send_message(chat_id=game.chat_id, text="🗳️ Голоса не засчитались. День пропущен.")
        await start_night(app, game)
        return

    max_votes = max(tally.values())
    top = [tid for tid, c in tally.items() if c == max_votes]

    if len(top) != 1:
        await app.bot.send_message(
            chat_id=game.chat_id,
            text="⚖️ Ничья по голосам! Никого не изгнали.\nПереходим к ночи."
        )
        await start_night(app, game)
        return

    eliminated_id = top[0]
    game.players[eliminated_id].alive = False
    eliminated = game.players[eliminated_id]

    msg = f"🚨 По итогам голосования изгнан(а): ☠️ {eliminated.name}\n"
    win = check_win(game)
    if win:
        game.phase = "ended"
        msg += "\n" + win + "\n\n" + "🎭 Роли:\n" + format_players_list(game, show_roles=True)
        await app.bot.send_message(chat_id=game.chat_id, text=msg)
        return

    await app.bot.send_message(chat_id=game.chat_id, text=msg)
    await start_night(app, game)


async def start_night(app: Application, game: GameState):
    game.phase = "night"
    game.votes.clear()
    game.mafia_target = None
    game.detective_check = None
    game.doctor_save = None
    game.night_done.clear()
    game.day_round += 1

    await app.bot.send_message(
        chat_id=game.chat_id,
        text=(
            f"🌙 Ночь {game.day_round} начинается.\n\n"
            "Мафия/детектив/доктор — действуйте в ЛС:\n"
            "🕶️ /kill <имя>\n"
            "🕵️ /check <имя>\n"
            "🩺 /protect <имя>\n\n"
            "Живые игроки:\n" + format_players_list(game)
        ),
    )


# =========================
# ЗАВЕРШИТЬ ИГРУ
# =========================
async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text("Завершать игру лучше в группе.")
        return

    chat_id = update.effective_chat.id
    game = get_game(chat_id)
    if not game:
        await update.message.reply_text("Нет активной игры.")
        return

    # разрешим завершить хосту или админу (упрощённо: хост)
    if update.effective_user.id != game.host_id:
        await update.message.reply_text("Только хост может завершить игру.")
        return

    game.phase = "ended"
    await update.message.reply_text(
        "🛑 Игра завершена.\n\n"
        "🎭 Роли:\n" + format_players_list(game, show_roles=True)
    )
    # чистим
    GAMES.pop(chat_id, None)


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TOKEN).build()

    # базовые
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # группа
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("leave", leave))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("vote", vote))
    app.add_handler(CommandHandler("endgame", endgame))

    # ЛС (ночные действия)
    app.add_handler(CommandHandler("kill", kill))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("protect", protect))

    logger.info("GAP Mafia bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
