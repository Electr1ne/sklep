import os
import random
import string
import asyncio
import time
import html
from datetime import datetime
from typing import Tuple, Optional, List

import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, ChatMemberUpdatedFilter, IS_ADMIN
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DEFAULT_CHANNEL = os.getenv("CHANNEL_USERNAME", "@GiftEZ")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не обнаружен в .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
MSK_TZ = pytz.timezone("Europe/Moscow")

BOT_USERNAME = ""


# --- FSM Состояния ---
class UserState(StatesGroup):
    entering_code = State()
    entering_rune_pass = State()


class CodeCreation(StatesGroup):
    choosing_points = State()
    typing_custom_points = State()
    choosing_code_type = State()
    typing_custom_code = State()


class SafeConfigFull(StatesGroup):
    choosing_name_mode = State()
    typing_custom_names = State()
    choosing_keys_mode = State()
    typing_custom_keys = State()
    choosing_costs_mode = State()
    typing_custom_costs = State()


class SafeAttemptsPolicy(StatesGroup):
    choosing_mode = State()
    choosing_attempts = State()
    typing_custom_attempts = State()
    choosing_ban_time = State()
    typing_custom_ban_time = State()


class AdminTimer(StatesGroup):
    entering_dates = State()


class PostEditing(StatesGroup):
    editing_start_post = State()
    editing_finish_post = State()


# --- Динамический шанс рулетки (Pity System) ---

def calculate_current_roulette_denominator(cfg) -> int:
    """
    Вычисляет динамический знаменатель шанса (1 к N).
    Чем больше сделано неудачных спинов или чем ближе конец ивента, тем меньше N.
    """
    spins = cfg["total_roulette_spins"] or 0
    base = 350

    # Снижение знаменателя в зависимости от числа круток
    # Каждые 10 круток знаменатель уменьшается на 10
    step_reduction = (spins // 10) * 12
    current_denom = max(2, base - step_reduction)

    # Фактор времени МСК (если близится конец, а медведя не выбили)
    if cfg["start_time_msk"] and cfg["end_time_msk"]:
        fmt = "%d.%m.%Y %H:%M"
        try:
            now_msk = datetime.now(MSK_TZ)
            start_dt = MSK_TZ.localize(datetime.strptime(cfg["start_time_msk"], fmt))
            end_dt = MSK_TZ.localize(datetime.strptime(cfg["end_time_msk"], fmt))
            total_duration = (end_dt - start_dt).total_seconds()
            elapsed = (now_msk - start_dt).total_seconds()

            if total_duration > 0 and elapsed > 0:
                progress = elapsed / total_duration
                if progress >= 0.85:  # Осталось 15% времени
                    current_denom = min(current_denom, 10)
                elif progress >= 0.70:  # Прошло 70% времени
                    current_denom = min(current_denom, 25)
                elif progress >= 0.50:
                    current_denom = min(current_denom, 75)
        except Exception:
            pass

    return max(2, current_denom)


# --- Генератор сложных кодов для рун ---

def generate_hardcore_rune_keys() -> List[str]:
    roots = ["SANGUIS", "CALVARIA", "TENEBRAE", "MORTIFER", "OBSCURUM", "OCCULTUS", "MALEDICTIO", "CUCURBITA"]
    selected = random.sample(roots, 4)
    keys = []
    for root in selected:
        salt = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        num = random.randint(10, 99)
        keys.append(f"{root}-{salt}-{num}")
    return keys


# --- Вспомогательные функции времени и текстов ---

def calculate_duration_str(start_str: Optional[str], end_str: Optional[str]) -> str:
    if not start_str or not end_str:
        return "до закрытия склепа"
    fmt = "%d.%m.%Y %H:%M"
    try:
        s_dt = datetime.strptime(start_str.strip(), fmt)
        e_dt = datetime.strptime(end_str.strip(), fmt)
        delta = e_dt - s_dt
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            return "до закрытия склепа"
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if hours > 0 and minutes > 0:
            return f"за {hours} ч. {minutes} мин."
        elif hours > 0:
            return f"за {hours} ч."
        else:
            return f"за {minutes} мин."
    except Exception:
        return "до закрытия склепа"


def get_target_channel_id(cfg):
    return cfg["channel_id"] if cfg["channel_id"] else DEFAULT_CHANNEL


def get_channel_post_keyboard() -> InlineKeyboardMarkup:
    url = f"https://t.me/{BOT_USERNAME}?start=event" if BOT_USERNAME else "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎃 Зайти в склеп / Начать охоту", url=url)]
    ])


def generate_start_post(cfg) -> str:
    start_str = cfg["start_time_msk"] or "Прямо сейчас"
    end_str = cfg["end_time_msk"] or "до полуночи"
    duration = calculate_duration_str(cfg["start_time_msk"], cfg["end_time_msk"])
    bot_link = f"@{BOT_USERNAME}" if BOT_USERNAME else "в нашем боте"

    if cfg["custom_start_post"]:
        return cfg["custom_start_post"].format(
            duration=duration,
            start_time=start_str,
            end_time=end_str,
            bot_link=bot_link
        )

    return (
        f"🎃 <b>ХЭЛЛОУИН В GIFTEZ: 3 РЕДКИХ МЕДВЕДЯ {duration.upper()}!</b>\n\n"
        f"⏳ <b>Время охоты:</b> с «{start_str}» до «{end_str}» (МСК).\n\n"
        "Склеп распахнут! На кону 3 уникальных Хэллоуинских Медведя. "
        "Никакого долгого ожидания — чистый соревновательный детектив на скорость!\n\n"
        "🐻 <b>КАК ЗАБРАТЬ МЕДВЕДЕЙ:</b>\n\n"
        "🎰 <b>1. Медведь «Сладость или Гадость» (Рулетка):</b>\n"
        "В боте ждет Колесо Судьбы. У каждого — <b>СТРОГО 1 ПОПЫТКА</b>. "
        "Сектор со «Сладостью» (медведем) — <b>ВСЕГО ОДИН НА ВЕСЬ КАНАЛ</b>! "
        "Кто первым выбивает его — забирает мишку, а рулетка навсегда блокируется для остальных!\n\n"
        "📜 <b>2. Медведь «Архивный Сталкер» (Очки):</b>\n"
        "Мы отредактировали старые посты в канале (за прошлые месяцы) и спрятали там секретные коды! "
        "Наводки будут выходить в ленте. "
        "⚡ <b>Коды ОДНОРАЗОВЫЕ</b> — очки достаются только первому, кто найдет и введет шифр в бота! Лидер топа к финалу заберет второго медведя.\n\n"
        "🔐 <b>3. Медведь «Разрушитель 4 Рун Сейфа»:</b>\n"
        "Сейф запечатан 4 сложными магическими рунами. За накопленные очки покупайте свитки-подсказки к рунам (при этом заработанные очки в лидерборде НЕ сгорают!). "
        "Кто первым деактивирует все 4 руны алтаря — взламывает сейф и уносит третьего медведя!\n\n"
        "📦 <b>О выдаче:</b> Медведь из рулетки может быть выдан сразу или в конце, "
        "остальные призы — строго после закрытия склепа.\n\n"
        f"🔗 <b>Переходи в бота по кнопке ниже:</b> {bot_link}\n\n"
        "🦇 <i>Включайте уведомления на канал — первая наводка на архивный тайник выйдет совсем скоро!</i>"
    )


def generate_finish_post(cfg) -> str:
    w1 = "Не разыгран"
    if cfg["bear1_winner_id"]:
        u1 = db.get_user(cfg["bear1_winner_id"])
        w1 = f"@{html.escape(u1['username'])}" if u1 else f"ID {cfg['bear1_winner_id']}"

    w3 = "Не взломан"
    if cfg["bear3_winner_id"]:
        u3 = db.get_user(cfg["bear3_winner_id"])
        w3 = f"@{html.escape(u3['username'])}" if u3 else f"ID {cfg['bear3_winner_id']}"

    leaders = db.get_leaderboard(1)
    w2 = f"@{html.escape(leaders[0]['username'])} (Собрано с кодов: {leaders[0]['total_earned_points']} 🍬)" if leaders else "Не определен"

    bot_link = f"@{BOT_USERNAME}" if BOT_USERNAME else "в боте"

    if cfg["custom_finish_post"]:
        return cfg["custom_finish_post"].format(
            w1=w1, w2=w2, w3=w3, bot_link=bot_link
        )

    return (
        "🌕 <b>СКЛЕП GIFTEZ ЗАКРЫТ: ИТОГИ ХЭЛЛОУИНСКОГО ИВЕНТА!</b>\n\n"
        "Время истекло, врата запечатаны, а все 3 редких медведя обрели своих законных владельцев:\n\n"
        f"🎰 <b>Медведь №1 (Рулетка «Сладость»):</b> {w1}\n"
        f"📜 <b>Медведь №2 (Топ сыщик архивов):</b> {w2}\n"
        f"🔐 <b>Медведь №3 (Взлом Рун Сейфа):</b> {w3}\n\n"
        "📦 <b>Выдача наград:</b>\n"
        "Если Медведь №1 не был передан сразу при дропе — все 3 приза вручаются прямо сейчас!\n"
        "Победителям отписать в ЛС администратору. Всем спасибо за охоту! 🎃"
    )


async def send_to_channel_safe(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> Tuple[bool, str]:
    cfg = db.get_event_config()
    target = get_target_channel_id(cfg)
    try:
        await bot.send_message(target, text, reply_markup=reply_markup, parse_mode="HTML")
        return True, "Успешно опубликовано в канал!"
    except Exception as e:
        return False, str(e)


# --- Инлайн-клавиатуры Пользователя ---

def get_main_menu(user_id: int, cfg):
    buttons = [
        [InlineKeyboardButton(text="🎰 Крутить рулетку (1 попытка)", callback_data="spin_roulette")],
        [InlineKeyboardButton(text="🔍 Ввести найденный код", callback_data="enter_promo_code")],
        [InlineKeyboardButton(text="🔐 Взлом Проклятого Сейфа", callback_data="open_safe_menu")],
        [
            InlineKeyboardButton(text="🏆 Лидерборд", callback_data="show_leaderboard"),
            InlineKeyboardButton(text="📜 Правила и призы", callback_data="show_rules")
        ]
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="adm_open_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« В главное меню", callback_data="back_to_main")]
    ])


def get_runes_altar_menu(user_id: int, cfg):
    user = db.get_user(user_id)
    bought = db.get_user_hints(user_id)

    rune_names = [
        (cfg["rune_1_name"], "rune_1_unlocked", 1),
        (cfg["rune_2_name"], "rune_2_unlocked", 2),
        (cfg["rune_3_name"], "rune_3_unlocked", 3),
        (cfg["rune_4_name"], "rune_4_unlocked", 4)
    ]

    buttons = []
    for title, field, idx in rune_names:
        is_unlocked = user[field] == 1
        st = "✅ Разрушена" if is_unlocked else "🔒 Активировать"
        buttons.append([InlineKeyboardButton(text=f"{title}: {st}", callback_data=f"break_rune_{idx}")])

    hint_row = []
    for i in range(1, 5):
        cost = cfg[f"hint_{i}_cost"]
        if i in bought:
            hint_row.append(InlineKeyboardButton(text=f"📜 {i} (Открыт)", callback_data=f"view_hint_{i}"))
        else:
            hint_row.append(InlineKeyboardButton(text=f"📜 {i} ({cost}🍬)", callback_data=f"buy_hint_{i}"))
    buttons.append(hint_row)
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Инлайн-клавиатура Админа ---

def get_admin_menu():
    cfg = db.get_event_config()
    status_emoji = "🟢 АКТИВЕН" if cfg["is_active"] else "🔴 ОСТАНОВЛЕН"
    safe_emoji = "🔓 ОТКРЫТ" if cfg["safe_opened"] else "🔒 ЗАКРЫТ"
    auto_post_emoji = "🟢 ВКЛ" if cfg["auto_channel_post"] else "🔴 ВЫКЛ (Ручной)"

    mode_text = "Индивидуальный (у каждого свой)" if cfg["safe_mode"] == "individual" else "ОБЩИЙ НА ВСЕХ"
    ch_info = cfg["channel_title"] if cfg["channel_title"] else DEFAULT_CHANNEL
    start_str = cfg["start_time_msk"] or "Вручную"
    end_str = cfg["end_time_msk"] or "Вручную"
    duration = calculate_duration_str(cfg["start_time_msk"], cfg["end_time_msk"])

    current_denom = calculate_current_roulette_denominator(cfg)
    spins = cfg["total_roulette_spins"] or 0

    text = (
        f"⚙️ <b>ПАНЕЛЬ АДМИНИСТРАТОРА GIFTEZ</b>\n\n"
        f"📢 Канал: <b>{html.escape(str(ch_info))}</b> (ID: {cfg['channel_id'] or 'По умолчанию'})\n"
        f"Статус ивента: <b>{status_emoji}</b>\n"
        f"Авто-постинг: <b>{auto_post_emoji}</b>\n"
        f"Тайминг МСК: «{start_str}» — «{end_str}» ({duration})\n\n"
        f"🎰 <b>Рулетка (Медведь №1):</b>\n"
        f"• Сделано круток: <b>{spins}</b>\n"
        f"• Текущий шанс дропа: <b>1 к {current_denom}</b> (повышается автоматически)\n"
        f"• Статус: {'✅ Выбит победителем' if cfg['bear1_claimed'] else '❌ Еще не найден'}\n\n"
        f"🔐 <b>Настройки Сейфа:</b>\n"
        f"• Доступ: <b>{safe_emoji}</b>\n"
        f"• Режим попыток: <b>{mode_text}</b>\n"
        f"• Лимит попыток: <b>{cfg['safe_attempts_limit']}</b> шт.\n"
        f"• Блокировка при провале: <b>на {cfg['safe_ban_duration_min']} мин.</b>\n"
        f"Бот: <b>@{BOT_USERNAME}</b>\n\n"
        f"Руны:\n"
        f"1. {cfg['rune_1_name']}: {cfg['rune_1_key']}\n"
        f"2. {cfg['rune_2_name']}: {cfg['rune_2_key']}\n"
        f"3. {cfg['rune_3_name']}: {cfg['rune_3_key']}\n"
        f"4. {cfg['rune_4_name']}: {cfg['rune_4_key']}\n\n"
        f"Медведь №3 (Сейф): {'✅ Взломан' if cfg['bear3_claimed'] else '❌ Закрыт'}"
    )

    buttons = [
        [
            InlineKeyboardButton(text="🟢 Запустить ивент", callback_data="adm_start_now"),
            InlineKeyboardButton(text="🔴 Остановить ивент", callback_data="adm_stop_now")
        ],
        [
            InlineKeyboardButton(text=f"📢 Авто-постинг: {auto_post_emoji}", callback_data="adm_toggle_autopost"),
            InlineKeyboardButton(text="🧪 Тест прав в канале", callback_data="adm_test_channel")
        ],
        [
            InlineKeyboardButton(text="⏰ Настроить МСК таймер", callback_data="adm_set_timer"),
            InlineKeyboardButton(text=f"🔐 Сейф: {'ЗАКРЫТЬ' if cfg['safe_opened'] else 'ОТКРЫТЬ'}",
                                 callback_data="adm_toggle_safe")
        ],
        [
            InlineKeyboardButton(text="🛡 Настройка попыток/блокировки Сейфа", callback_data="adm_setup_safe_policy"),
            InlineKeyboardButton(text="🔮 Настройка Рун Сейфа (Авто/Свои)", callback_data="adm_setup_safe_flow")
        ],
        [
            InlineKeyboardButton(text="📝 Редактировать посты", callback_data="adm_edit_posts_menu"),
            InlineKeyboardButton(text="👁 Предпросмотры", callback_data="adm_previews_menu")
        ],
        [
            InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm_create_code_flow"),
            InlineKeyboardButton(text="📋 Список кодов", callback_data="adm_list_codes")
        ],
        [
            InlineKeyboardButton(text="🏆 Итоги и Пост в канал", callback_data="adm_finish_results"),
            InlineKeyboardButton(text="« Выйти в меню бота", callback_data="back_to_main")
        ]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Добавление бота в канал (привязка) ---

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_ADMIN))
async def on_bot_added_to_channel(event: ChatMemberUpdated):
    if event.chat.type in ["channel", "supergroup"]:
        channel_id = event.chat.id
        channel_title = event.chat.title or "Без названия"
        channel_username = f"@{event.chat.username}" if event.chat.username else "Приватный"

        db.save_bound_channel(channel_id, channel_title)

        notify_text = (
            "🎉 <b>БОТ УСПЕШНО ПРИВЯЗАН К КАНАЛУ!</b>\n\n"
            f"📢 <b>Название:</b> {html.escape(channel_title)}\n"
            f"🆔 <b>ID канала:</b> {channel_id}\n"
            f"🔗 <b>Username:</b> {channel_username}\n\n"
            "Права администратора подтверждены! Бот готов отправлять стартовый пост с кнопкой, "
            "новости о первом медведе и финальные итоги."
        )
        try:
            await bot.send_message(ADMIN_ID, notify_text, parse_mode="HTML")
        except Exception as e:
            print(f"Не удалось отправить уведомление админу: {e}")


# --- Планировщик времени МСК ---

async def check_msk_schedule():
    cfg = db.get_event_config()
    if not cfg["start_time_msk"] or not cfg["end_time_msk"]:
        return

    now_msk = datetime.now(MSK_TZ)
    fmt = "%d.%m.%Y %H:%M"

    try:
        start_dt = MSK_TZ.localize(datetime.strptime(cfg["start_time_msk"], fmt))
        end_dt = MSK_TZ.localize(datetime.strptime(cfg["end_time_msk"], fmt))

        if start_dt <= now_msk < end_dt:
            if not cfg["is_active"]:
                db.set_event_status(True)
                cfg = db.get_event_config()
                await bot.send_message(ADMIN_ID, "⏰ <b>МСК-таймер:</b> Ивент АВТОМАТИЧЕСКИ запущен!")
                post_text = generate_start_post(cfg)
                kb_post = get_channel_post_keyboard()
                if cfg["auto_channel_post"]:
                    ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
                    await bot.send_message(ADMIN_ID, f"📢 <b>Авто-пост старта в канал:</b> {msg}")
                else:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📢 Опубликовать старт в канал", callback_data="adm_send_post_start")]
                    ])
                    await bot.send_message(ADMIN_ID,
                                           f"⏰ <b>Ивент запущен по расписанию!</b> Опубликовать пост в канал?\n\n{post_text}",
                                           reply_markup=kb, parse_mode="HTML")

        elif now_msk >= end_dt:
            if cfg["is_active"]:
                db.set_event_status(False)
                cfg = db.get_event_config()
                await bot.send_message(ADMIN_ID, "⏰ <b>МСК-таймер:</b> Время истекло! Ивент АВТОМАТИЧЕСКИ остановлен.")
                post_text = generate_finish_post(cfg)
                kb_post = get_channel_post_keyboard()
                if cfg["auto_channel_post"]:
                    ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
                    await bot.send_message(ADMIN_ID, f"📢 <b>Авто-пост итогов в канал:</b> {msg}")
                else:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📢 Опубликовать итоги в канал",
                                              callback_data="adm_send_post_finish")]
                    ])
                    await bot.send_message(ADMIN_ID, f"🏁 <b>Ивент завершен!</b> Опубликовать итоги?\n\n{post_text}",
                                           reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка проверки расписания: {e}")


# --- Пользовательская часть ---

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    db.register_user(user_id, username)

    cfg = db.get_event_config()
    user = db.get_user(user_id)

    if not cfg["is_active"] and user_id != ADMIN_ID:
        start_txt = cfg['start_time_msk'] or "Объявим в канале"
        end_txt = cfg['end_time_msk'] or "в конце ивента"
        duration = calculate_duration_str(cfg["start_time_msk"], cfg["end_time_msk"])
        await message.answer(
            "🕯 <b>Склеп GiftEZ сейчас запечатан!</b>\n\n"
            "Охота еще не началась либо уже завершилась.\n\n"
            f"📅 <b>Время проведения (МСК):</b>\n"
            f"• Старт: {start_txt}\n"
            f"• Финиш: {end_txt} ({duration})\n\n"
            "Следите за новостями в канале проекта!",
            parse_mode="HTML"
        )
        return

    await message.answer(
        f"🕯 <b>ДОБРО ПОЖАЛОВАТЬ В СКЛЕП GIFTEZ!</b>\n\n"
        f"👤 Игрок: @{html.escape(username or 'Странник')}\n"
        f"🍬 Доступно для подсказок: <b>{user['points']} 🍬</b>\n"
        f"🏆 В зачете лидерборда: <b>{user['total_earned_points']} 🍬</b>\n\n"
        f"<i>Подсказка: очки, потраченные на свитки, НЕ сгорают из лидерборда!</i>\n\n"
        f"Ищи спрятанные шифры в архивных постах канала, крути рулетку и разрушай защитные руны Сейфа!",
        reply_markup=get_main_menu(user_id, cfg),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "back_to_main")
async def cb_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = db.get_event_config()
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    await callback.message.edit_text(
        f"🕯 <b>ГЛАВНЫЙ ЗАЛ СКЛЕПА</b>\n\n"
        f"🍬 Доступно для подсказок: <b>{user['points']} 🍬</b>\n"
        f"🏆 В зачете лидерборда: <b>{user['total_earned_points']} 🍬</b>\n\n"
        f"Выбирай испытание:",
        reply_markup=get_main_menu(user_id, cfg),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "show_rules")
async def cb_rules(callback: CallbackQuery):
    cfg = db.get_event_config()
    duration = calculate_duration_str(cfg["start_time_msk"], cfg["end_time_msk"])
    policy_str = "у каждого игрока свои" if cfg["safe_mode"] == "individual" else "ОБЩИЕ НА ВЕСЬ КАНАЛ"
    rules_text = (
        f"🎃 <b>ПРАВИЛА ИВЕНТА ({duration.upper()}):</b>\n\n"
        "🐻 <b>Медведь №1 — «Сладость или Гадость»</b>\n"
        "У каждого строго <b>1 попытка</b>. В рулетке ровно 1 медведь на всех! "
        "Кто выбьет первым — забирает приз, а рулетка блокируется навсегда. "
        "<b>Шанс выпадения растет</b> с каждым вращением и к концу ивента!\n\n"
        "🐻 <b>Медведь №2 — «Архивный Сталкер»</b>\n"
        "Мы редактируем старые посты в канале и прячем одноразовые коды. "
        "Очки достаются только первому активировавшему. Лидер общего зачета очков забирает медведя! "
        "<b>Важно:</b> траты очков на подсказки НЕ уменьшают ваш счет в лидерборде!\n\n"
        "🐻 <b>Медведь №3 — «Разрушитель 4 Рун Сейфа»</b>\n"
        f"Сейф защищен 4 древними рунами. Попытки ввода ({policy_str}): <b>{cfg['safe_attempts_limit']} шт.</b> "
        f"При исчерпании сейф блокируется ровно на <b>{cfg['safe_ban_duration_min']} минут</b>! "
        "Очки тратятся на покупку свитков с подсказками. "
        "Кто первым деактивирует все 4 руны алтаря — взламывает сейф и забирает третьего медведя!\n\n"
        "📦 <i>Один участник может выиграть только одного медведя. Медведь из рулетки может быть выдан сразу либо по окончанию, остальные — строго после закрытия склепа!</i>"
    )
    await callback.message.edit_text(rules_text, reply_markup=get_back_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "show_leaderboard")
async def cb_leaderboard(callback: CallbackQuery):
    leaders = db.get_leaderboard(10)
    lines = ["🏆 <b>ТОП-10 СЫЩИКОВ ПО ОЧКАМ (СУММАРНО С КОДОВ):</b>\n"]
    if not leaders:
        lines.append("<i>Пока никто не активировал ни одного кода...</i>")
    else:
        for idx, row in enumerate(leaders, 1):
            lines.append(
                f"{idx}. @{html.escape(row['username'])} — <b>{row['total_earned_points']} 🍬</b> (Баланс: {row['points']})")
    lines.append(
        "\n<i>Лидер списка к моменту закрытия забирает Медведя №2!</i>\n<i>Покупка подсказок не уменьшает заработанные в зачет очки.</i>")
    await callback.message.edit_text("\n".join(lines), reply_markup=get_back_menu(), parse_mode="HTML")


# --- Медведь №1: Рулетка с прогрессивным шансом ---

@dp.callback_query(F.data == "spin_roulette")
async def cb_spin_animated(callback: CallbackQuery):
    user_id = callback.from_user.id
    cfg = db.get_event_config()
    user = db.get_user(user_id)
    now_ts = int(time.time())

    if user["has_spun"] == 1:
        await callback.answer("⚠️ Твоя единственная попытка уже использована!", show_alert=True)
        return

    if cfg["bear1_claimed"] == 1:
        await callback.answer("💀 Увы! Единственного Медведя из рулетки уже забрали!", show_alert=True)
        return

    # Засчитываем попытку в общий пул счетчика круток
    db.record_spin_attempt(now_ts)
    cfg = db.get_event_config()

    frames = [
        "🎰 <b>Колесо Склепа начинает вращаться...</b>\n\n[ 🦇 | 🍬 | 🕷️ ]\n<i>Шестеренки крутятся...</i>",
        "🎰 <b>Колесо Склепа ускоряется...</b>\n\n[ 🎃 | 💀 | 🐻 ]\n<i>Призрачная сила выбирает...</i>",
        "🎰 <b>Колесо Склепа замирает...</b>\n\n[ 🕯️ | 👁️ | 🕸️ ]\n<i>Стрелка останавливается...</i>"
    ]

    for frame in frames:
        await callback.message.edit_text(frame, parse_mode="HTML")
        await asyncio.sleep(0.7)

    # Расчет прогрессивного шанса
    current_denom = calculate_current_roulette_denominator(cfg)
    won = (random.randint(1, current_denom) == 1)

    if won:
        db.set_spin_done(user_id, won_bear=True)
        uname = callback.from_user.username or "Игрок"
        await callback.message.edit_text(
            "🎉 <b>НЕВЕРОЯТНО!!! ВЫПАЛА СЛАДОСТЬ!</b>\n\n"
            "🐻 <b>ТЫ ЗАБИРАЕШЬ ХЭЛЛОУИНСКОГО МЕДВЕДЯ №1!</b>\n\n"
            "Сектор «Сладость» запечатан навсегда для всех остальных.\n"
            "Организатор получил уведомление. Медведь может быть выдан сразу или по завершению ивента!",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )

        notify_admin = (
            f"🚨 <b>МЕДВЕДЬ №1 ВЫБИТ В РУЛЕТКЕ!</b>\n\n"
            f"Победитель: @{html.escape(uname)} (ID: {user_id})\n"
            f"Выбит на {cfg['total_roulette_spins']} прокруте (Шанс был 1 к {current_denom}).\n"
            f"Рулетка заблокирована для остальных участников."
        )

        channel_post = (
            "🚨 <b>СЕКТОР «СЛАДОСТЬ» СОРВАН! МЕДВЕДЬ №1 НАШЕЛ ХОЗЯИНА!</b>\n\n"
            f"Счастливчик @{html.escape(uname)} только что прокрутил рулетку в боте и выбил <b>Хэллоуинского Медведя №1</b>!\n\n"
            "Рулетка закрыта навсегда. Но охота продолжается: впереди борьба за Медведя №2 (архивные коды) и Медведя №3 (взлом Сейфа)!"
        )

        kb_post = get_channel_post_keyboard()
        if cfg["auto_channel_post"]:
            ok, msg = await send_to_channel_safe(channel_post, reply_markup=kb_post)
            await bot.send_message(ADMIN_ID, f"{notify_admin}\n\n📢 <i>Авто-пост в канал: {msg}</i>", parse_mode="HTML")
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Опубликовать новость в канал", callback_data="adm_send_bear1_post")]
            ])
            await bot.send_message(ADMIN_ID, f"{notify_admin}\n\nОпубликовать новость в канал?", reply_markup=kb,
                                   parse_mode="HTML")
    else:
        db.set_spin_done(user_id, won_bear=False)
        await callback.message.edit_text(
            "🕸️ <b>ТЕБЕ ДОСТАЛАСЬ ГАДОСТЬ!</b>\n\n"
            "Из ящика выскочила горсть могильной шелухи и ядовитый паук 🕷️.\n"
            "Попытка исчерпана! Но ты все еще можешь добыть Медведя №2 и №3 за поиск кодов и взлом Сейфа!",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )


# --- Медведь №2: Ввод кода ---

@dp.callback_query(F.data == "enter_promo_code")
async def cb_enter_promo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.entering_code)
    await callback.message.edit_text(
        "🔍 <b>ВВОД АРХИВНОГО ШИФРА</b>\n\n"
        "Отправьте найденный одноразовый шифр из старых постов канала ответным сообщением.\n"
        "<i>Каждый код уникален и активируется ровно 1 раз!</i>",
        reply_markup=get_back_menu(),
        parse_mode="HTML"
    )


@dp.message(UserState.entering_code)
async def process_user_code(message: Message, state: FSMContext):
    code_text = message.text.strip()
    user_id = message.from_user.id

    success, reason, pts = db.claim_promo_code(code_text, user_id)
    await state.clear()

    if success:
        user = db.get_user(user_id)
        await message.answer(
            f"🎉 <b>ШИФР ПРИНЯТ!</b>\n\n"
            f"Ты первым расшифровал тайник и получаешь <b>+{pts} 🍬 Candy Points</b>!\n"
            f"Твой зачет в лидерборде: <b>{user['total_earned_points']} 🍬</b>.",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )
        await bot.send_message(
            ADMIN_ID,
            f"🗝️ <b>Архивный код активирован!</b>\n"
            f"Код: {html.escape(code_text.upper())} (+{pts} очков)\n"
            f"Забрал: @{html.escape(message.from_user.username or 'Странник')} (ID: {user_id})",
            parse_mode="HTML"
        )
    elif reason == "already_used":
        await message.answer(
            "💀 <b>ОПОЗДАЛ!</b>\n\nЭтот шифр уже активировал другой сыщик. Код сгорел!",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "❌ <b>Шифр не существует!</b> Проверьте написание.",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )


# --- Медведь №3: Руны Сейфа ---

@dp.callback_query(F.data == "open_safe_menu")
async def cb_open_safe(callback: CallbackQuery):
    cfg = db.get_event_config()
    user_id = callback.from_user.id
    user = db.get_user(user_id)

    if not cfg["safe_opened"] and user_id != ADMIN_ID:
        await callback.answer("🔒 Сейф запечатан админом! Доступ откроется позже.", show_alert=True)
        return

    if cfg["bear3_claimed"] == 1:
        await callback.message.edit_text(
            "🔓 <b>ПРОКЛЯТЫЙ СЕЙФ УЖЕ ВЗЛОМАН!</b>\n\n"
            "Все 4 руны алтаря пали. Медведя №3 забрал сильнейший игрок.",
            reply_markup=get_back_menu(),
            parse_mode="HTML"
        )
        return

    now_ts = int(time.time())
    is_banned, rem_sec, attempts = db.get_safe_status_for_user(user_id, now_ts)
    if is_banned:
        rem_min = (rem_sec // 60) + 1
        who_banned = "Сейф временно заблокирован ДЛЯ ВСЕХ!" if cfg[
                                                                   "safe_mode"] == "global" else "Твой доступ к Сейфу временно заблокирован!"
        await callback.answer(f"🚫 {who_banned} Жди еще {rem_min} мин.", show_alert=True)
        return

    mode_expl = "У каждого участника личный лимит" if cfg[
                                                          "safe_mode"] == "individual" else "⚠️ ОБЩИЙ ЛИМИТ НА ВСЕХ УЧАСТНИКОВ"

    await callback.message.edit_text(
        f"🔐 <b>АЛТАРЬ 4 МАГИЧЕСКИХ РУН (ВЗЛОМ СЕЙФА)</b>\n\n"
        f"Сейф защищен 4 печатями. Деактивируйте каждую руну ее заклинанием.\n\n"
        f"⚙️ <b>Режим защиты:</b> {mode_expl}\n"
        f"🛡 <b>Осталось попыток:</b> <b>{attempts} из {cfg['safe_attempts_limit']}</b>\n"
        f"⏳ <b>Блокировка при ошибках:</b> на <b>{cfg['safe_ban_duration_min']} мин.</b> ({'для всех' if cfg['safe_mode'] == 'global' else 'только для вас'})\n\n"
        f"🍬 Баланс для свитков: <b>{user['points']} 🍬</b>\n"
        f"🏆 Общий счет в лидерборде: <b>{user['total_earned_points']} 🍬</b>\n\n"
        f"<i>Покупайте свитки за очки внизу, чтобы узнать ключевые формулы рун:</i>",
        reply_markup=get_runes_altar_menu(user_id, cfg),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("buy_hint_"))
async def cb_buy_hint(callback: CallbackQuery):
    cfg = db.get_event_config()
    if not cfg["safe_opened"] and callback.from_user.id != ADMIN_ID:
        await callback.answer("🔒 Сейф еще закрыт!", show_alert=True)
        return

    hint_no = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    cost = cfg[f"hint_{hint_no}_cost"]

    success = db.buy_hint(user_id, hint_no, cost)
    if not success:
        await callback.answer(f"❌ Недостаточно очков! Нужно {cost} 🍬", show_alert=True)
        return

    await callback.answer("✅ Свиток расшифрован! Ваши баллы в лидерборде сохранились.")
    await cb_open_safe(callback)


@dp.callback_query(F.data.startswith("view_hint_"))
async def cb_view_hint(callback: CallbackQuery):
    hint_no = int(callback.data.split("_")[-1])
    cfg = db.get_event_config()
    hint_text = cfg[f"hint_{hint_no}_text"]
    await callback.answer(f"📜 Свиток {hint_no}:\n{hint_text}", show_alert=True)


@dp.callback_query(F.data.startswith("break_rune_"))
async def cb_break_rune_prompt(callback: CallbackQuery, state: FSMContext):
    cfg = db.get_event_config()
    if not cfg["safe_opened"] and callback.from_user.id != ADMIN_ID:
        await callback.answer("🔒 Сейф еще запечатан админом!", show_alert=True)
        return

    if cfg["bear3_claimed"] == 1:
        await callback.answer("Сейф уже взломан!", show_alert=True)
        return

    now_ts = int(time.time())
    is_banned, rem_sec, attempts = db.get_safe_status_for_user(callback.from_user.id, now_ts)
    if is_banned:
        rem_min = (rem_sec // 60) + 1
        who = "Сейф заблокирован ДЛЯ ВСЕХ!" if cfg["safe_mode"] == "global" else "Твой доступ к Сейфу заблокирован!"
        await callback.answer(f"🚫 {who} Жди еще {rem_min} мин.", show_alert=True)
        return

    rune_idx = int(callback.data.split("_")[-1])
    user = db.get_user(callback.from_user.id)

    if user[f"rune_{rune_idx}_unlocked"] == 1:
        await callback.answer("Эта руна уже разрушена!", show_alert=True)
        return

    await state.set_state(UserState.entering_rune_pass)
    await state.update_data(target_rune=rune_idx)

    name = cfg[f"rune_{rune_idx}_name"]
    await callback.message.edit_text(
        f"⚡ <b>ДЕАКТИВАЦИЯ РУНЫ #{rune_idx} ({name})</b>\n\n"
        f"Отправьте магическую формулу/заклинание этой руны ответным сообщением.\n\n"
        f"⚠️ Осторожно: неверный ввод тратит попытки (осталось: <b>{attempts}</b> шт.)!\n"
        f"<i>Код руны защищен древним гримуаром. Подсказку к нему можно открыть в свитках за очки!</i>",
        reply_markup=get_back_menu(),
        parse_mode="HTML"
    )


@dp.message(UserState.entering_rune_pass)
async def process_rune_pass(message: Message, state: FSMContext):
    cfg = db.get_event_config()
    user_id = message.from_user.id
    now_ts = int(time.time())

    if not cfg["safe_opened"] and user_id != ADMIN_ID:
        await state.clear()
        await message.answer("🔒 Сейф запечатан админом! Попытка отклонена.", reply_markup=get_back_menu())
        return

    is_banned, rem_sec, _ = db.get_safe_status_for_user(user_id, now_ts)
    if is_banned:
        await state.clear()
        rem_min = (rem_sec // 60) + 1
        who = "Сейф заблокирован для всех!" if cfg["safe_mode"] == "global" else "Ваш доступ к Сейфу заблокирован!"
        await message.answer(f"🚫 {who} Попытка не принята. Ожидайте {rem_min} мин.", reply_markup=get_back_menu())
        return

    data = await state.get_data()
    rune_idx = data.get("target_rune", 1)

    attempt = message.text.strip().upper()
    correct_key = cfg[f"rune_{rune_idx}_key"].strip().upper()
    await state.clear()

    if attempt == correct_key:
        db.unlock_user_rune(user_id, rune_idx)

        if db.check_all_runes_unlocked(user_id):
            db.set_safe_winner(user_id)
            await message.answer(
                "🎉 <b>ВСЕ 4 РУНЫ РАЗРУШЕНЫ! ПЕЧАТИ ПАЛИ!</b>\n\n"
                "🐻 <b>ТЫ ВЗЛОМАЛ ПРОКЛЯТЫЙ СЕЙФ И ЗАБИРАЕШЬ МЕДВЕДЯ №3!</b>\n\n"
                "Организатор получил сигнал о падении алтаря. Медведь твой!",
                reply_markup=get_back_menu(),
                parse_mode="HTML"
            )
            await bot.send_message(
                ADMIN_ID,
                f"🔓 <b>СЕЙФ ВЗЛОМАН! ПАЛИ ВСЕ 4 РУНЫ!</b>\n\n"
                f"Победитель: @{html.escape(message.from_user.username or 'Игрок')} (ID: {user_id})\n"
                f"Медведь №3 нашел владельца!",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"✨ <b>Руна #{rune_idx} успешно рассеяна!</b>\nОсталось разрушить оставшиеся печати на алтаре.",
                reply_markup=get_back_menu(),
                parse_mode="HTML"
            )
    else:
        ban_seconds = cfg["safe_ban_duration_min"] * 60
        ban_timestamp = now_ts + ban_seconds

        rem_attempts, did_ban = db.deduct_safe_attempt_poly(user_id, ban_timestamp)

        if did_ban:
            if cfg["safe_mode"] == "global":
                await message.answer(
                    f"🚫 <b>КРИТИЧЕСКИЙ СБОЙ АЛТАРЯ!</b>\n\n"
                    f"Исчерпаны все общие попытки! Сейф заблокирован ДЛЯ ВСЕХ на <b>{cfg['safe_ban_duration_min']} минут</b>!",
                    reply_markup=get_back_menu(),
                    parse_mode="HTML"
                )
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Внимание:</b> Игрок @{html.escape(message.from_user.username or 'Игрок')} потратил последнюю общую попытку. Сейф заблокирован ДЛЯ ВСЕХ на {cfg['safe_ban_duration_min']} мин!"
                )
            else:
                await message.answer(
                    f"🚫 <b>МАГИЧЕСКИЙ УДАР!</b>\n\n"
                    f"Вы исчерпали все {cfg['safe_attempts_limit']} попыток. Ваш личный доступ к Сейфу заблокирован на <b>{cfg['safe_ban_duration_min']} минут</b>!",
                    reply_markup=get_back_menu(),
                    parse_mode="HTML"
                )
        else:
            scope_str = "общих" if cfg["safe_mode"] == "global" else "ваших личных"
            await message.answer(
                f"❌ <b>Заклинание не сработало!</b> Ошибка формулы.\n"
                f"Осталось {scope_str} попыток: <b>{rem_attempts} из {cfg['safe_attempts_limit']}</b>.",
                reply_markup=get_back_menu(),
                parse_mode="HTML"
            )


# ==========================================
#         ПАНЕЛЬ АДМИНИСТРАТОРА
# ==========================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, kb = get_admin_menu()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_open_panel")
async def cb_adm_open_panel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_toggle_autopost")
async def adm_toggle_autopost(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    new_val = db.toggle_auto_post()
    await callback.answer(f"Авто-постинг {'ВКЛЮЧЕН' if new_val else 'ВЫКЛЮЧЕН (Ручной режим)'}")
    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_test_channel")
async def adm_test_channel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    target = get_target_channel_id(cfg)
    try:
        sent = await bot.send_message(
            target,
            "🧪 <b>Тест подключения GiftEZ Halloween Bot!</b>\n<i>Сообщение удалится автоматически...</i>",
            reply_markup=get_channel_post_keyboard(),
            parse_mode="HTML"
        )
        await asyncio.sleep(4)
        await bot.delete_message(target, sent.message_id)
        await callback.answer("✅ Тест успешен! Бот имеет полные права на отправку постов и кнопок в канал.",
                              show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка отправки в канал:\n{e}", show_alert=True)


@dp.callback_query(F.data == "adm_start_now")
async def adm_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    db.set_event_status(True)
    cfg = db.get_event_config()
    await callback.answer("🟢 Ивент запущен вручную!")

    post_text = generate_start_post(cfg)
    kb_post = get_channel_post_keyboard()
    if cfg["auto_channel_post"]:
        ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
        await callback.message.answer(f"📢 <b>Авто-пост старта:</b> {msg}")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Опубликовать старт в канал сейчас", callback_data="adm_send_post_start")]
        ])
        await callback.message.answer(f"🟢 <b>Ивент запущен!</b> Опубликовать пост в канал?\n\n{post_text}",
                                      reply_markup=kb, parse_mode="HTML")

    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_stop_now")
async def adm_stop(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    db.set_event_status(False)
    cfg = db.get_event_config()
    await callback.answer("🔴 Ивент остановлен вручную!")

    post_text = generate_finish_post(cfg)
    kb_post = get_channel_post_keyboard()
    if cfg["auto_channel_post"]:
        ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
        await callback.message.answer(f"📢 <b>Авто-пост итогов:</b> {msg}")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Опубликовать итоги в канал сейчас", callback_data="adm_send_post_finish")]
        ])
        await callback.message.answer(f"🔴 <b>Ивент остановлен!</b> Опубликовать итоги?\n\n{post_text}", reply_markup=kb,
                                      parse_mode="HTML")

    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_toggle_safe")
async def adm_toggle_safe(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    new_state = not cfg["safe_opened"]
    db.set_safe_status(new_state)
    await callback.answer("🔓 Сейф ОТКРЫТ для игроков!" if new_state else "🔒 Сейф ЗАКРЫТ от игроков!")
    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# --- Настройка Политики попыток и блокировок Сейфа ---

@dp.callback_query(F.data == "adm_setup_safe_policy")
async def adm_setup_policy_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(SafeAttemptsPolicy.choosing_mode)

    buttons = [
        [InlineKeyboardButton(text="👤 У каждого игрока свои попытки", callback_data="policy_mode_ind")],
        [InlineKeyboardButton(text="👥 ОБЩИЙ счетчик попыток на весь канал", callback_data="policy_mode_glob")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    txt = (
        "🛡 <b>НАСТРОЙКА ПОЛИТИКИ СЕЙФА (ШАГ 1/3)</b>\n\n"
        "Выберите режим счетчика попыток:\n\n"
        "• <b>Индивидуальный:</b> каждый пробует сам. Провал блокирует сейф только для ошибшегося.\n"
        "• <b>Общий на всех:</b> пул попыток один на всех. Ошибка на нуле блокирует сейф для всего канала сразу!"
    )
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(SafeAttemptsPolicy.choosing_mode, F.data.startswith("policy_mode_"))
async def adm_policy_got_mode(callback: CallbackQuery, state: FSMContext):
    mode = "individual" if callback.data == "policy_mode_ind" else "global"
    await state.update_data(safe_mode=mode)
    await state.set_state(SafeAttemptsPolicy.choosing_attempts)

    buttons = [
        [InlineKeyboardButton(text="3 попытки", callback_data="att_3"),
         InlineKeyboardButton(text="5 попыток", callback_data="att_5")],
        [InlineKeyboardButton(text="10 попыток", callback_data="att_10"),
         InlineKeyboardButton(text="15 попыток", callback_data="att_15")],
        [InlineKeyboardButton(text="✍️ Вписать своё число", callback_data="att_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    await callback.message.edit_text(
        "🛡 <b>НАСТРОЙКА ПОПЫТОК (ШАГ 2/3)</b>\n\n"
        "Сколько попыток дается до блокировки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(SafeAttemptsPolicy.choosing_attempts, F.data.startswith("att_"))
async def adm_policy_got_attempts(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("_")[-1]
    if val == "custom":
        await state.set_state(SafeAttemptsPolicy.typing_custom_attempts)
        await callback.message.edit_text("Отправьте число попыток сообщением (например: 7):")
        return
    await state.update_data(attempts=int(val))
    await adm_policy_step3_ask_ban(callback, state)


@dp.message(SafeAttemptsPolicy.typing_custom_attempts)
async def adm_policy_typed_attempts(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        val = int(message.text.strip())
        if val <= 0: raise ValueError
        await state.update_data(attempts=val)
        await adm_policy_step3_ask_ban(message, state)
    except Exception:
        await message.answer("Введите целое положительное число попыток!")


async def adm_policy_step3_ask_ban(event, state: FSMContext):
    await state.set_state(SafeAttemptsPolicy.choosing_ban_time)
    buttons = [
        [InlineKeyboardButton(text="5 минут", callback_data="ban_5"),
         InlineKeyboardButton(text="15 минут", callback_data="ban_15")],
        [InlineKeyboardButton(text="30 минут", callback_data="ban_30"),
         InlineKeyboardButton(text="60 минут", callback_data="ban_60")],
        [InlineKeyboardButton(text="✍️ Вписать своё число минут", callback_data="ban_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    txt = (
        "🛡 <b>ВРЕМЯ БЛОКИРОВКИ (ШАГ 3/3)</b>\n\n"
        "На сколько минут блокировать Сейф при исчерпании попыток?"
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                                      parse_mode="HTML")
    else:
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(SafeAttemptsPolicy.choosing_ban_time, F.data.startswith("ban_"))
async def adm_policy_got_ban(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("_")[-1]
    if val == "custom":
        await state.set_state(SafeAttemptsPolicy.typing_custom_ban_time)
        await callback.message.edit_text("Отправьте время блокировки в минутах (например: 45):")
        return
    data = await state.get_data()
    db.update_safe_attempts_policy(data["safe_mode"], data["attempts"], int(val))
    await state.clear()
    await callback.answer("✅ Настройки Сейфа сохранены!", show_alert=True)
    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(SafeAttemptsPolicy.typing_custom_ban_time)
async def adm_policy_typed_ban(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        val = int(message.text.strip())
        if val <= 0: raise ValueError
        data = await state.get_data()
        db.update_safe_attempts_policy(data["safe_mode"], data["attempts"], val)
        await state.clear()
        await message.answer(f"✅ Настройки сохранены! Попыток: {data['attempts']}, Блокировка: {val} мин.")
        text, kb = get_admin_menu()
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer("Введите целое положительное число минут!")


# --- Пошаговая настройка Рун Сейфа ---

@dp.callback_query(F.data == "adm_setup_safe_flow")
async def adm_safe_step1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(SafeConfigFull.choosing_name_mode)

    buttons = [
        [InlineKeyboardButton(text="⚡ Авто-названия (Крови, Праха, Тьмы, Тыквы)", callback_data="runes_names_auto")],
        [InlineKeyboardButton(text="✍️ Задать свои 4 названия", callback_data="runes_names_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    await callback.message.edit_text("<b>Шаг 1: Названия 4 Рун Сейфа</b>\n\nВыберите вариант:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(SafeConfigFull.choosing_name_mode, F.data == "runes_names_auto")
async def adm_safe_names_auto(callback: CallbackQuery, state: FSMContext):
    default_names = ["Крови 🩸", "Праха 💀", "Тьмы 👁️", "Тыквы 🎃"]
    await state.update_data(names=default_names)
    await adm_safe_step2_ask_keys(callback, state)


@dp.callback_query(SafeConfigFull.choosing_name_mode, F.data == "runes_names_custom")
async def adm_safe_names_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SafeConfigFull.typing_custom_names)
    await callback.message.edit_text(
        "<b>Шаг 1: Ввод названий рун</b>\n\n"
        "Отправьте ровно 4 названия через вертикальную черту «|»:\n"
        "<i>Пример: Руна Огня 🔥 | Руна Льда ❄️ | Руна Бездны 🌑 | Руна Призрака 👻</i>",
        parse_mode="HTML"
    )


@dp.message(SafeConfigFull.typing_custom_names)
async def adm_safe_got_custom_names(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) != 4:
        await message.answer("Ошибка! Нужно ровно 4 названия, разделенных символом «|». Попробуйте снова:")
        return
    await state.update_data(names=parts)
    await adm_safe_step2_ask_keys(message, state)


async def adm_safe_step2_ask_keys(event, state: FSMContext):
    await state.set_state(SafeConfigFull.choosing_keys_mode)
    buttons = [
        [InlineKeyboardButton(text="⚡ Авто (Сложные криптические коды x10)", callback_data="runes_keys_hardcore_auto")],
        [InlineKeyboardButton(text="✍️ Ввести свои 4 кода рун", callback_data="runes_keys_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    txt = (
        "<b>Шаг 2: Заклинания/Коды рун</b>\n\n"
        "• <b>Авто:</b> будут сгенерированы оккультные формулы (например: SANGUIS-9X2-74), которые невозможно подобрать случайно.\n"
        "• <b>Свои:</b> вы впишете 4 собственных секретных слова."
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                                      parse_mode="HTML")
    else:
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(SafeConfigFull.choosing_keys_mode, F.data == "runes_keys_hardcore_auto")
async def adm_safe_keys_auto(callback: CallbackQuery, state: FSMContext):
    hard_keys = generate_hardcore_rune_keys()
    await state.update_data(keys=hard_keys)
    await adm_safe_step3_ask_costs(callback, state)


@dp.callback_query(SafeConfigFull.choosing_keys_mode, F.data == "runes_keys_custom")
async def adm_safe_keys_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SafeConfigFull.typing_custom_keys)
    await callback.message.edit_text(
        "<b>Шаг 2: Ввод собственных кодов рун</b>\n\n"
        "Отправьте 4 кода через пробел:\n"
        "<i>Пример: AETERNITAS-91 MORTEM-44 NOCTIS-88 ARCANA-73</i>",
        parse_mode="HTML"
    )


@dp.message(SafeConfigFull.typing_custom_keys)
async def adm_safe_got_custom_keys(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    keys = message.text.strip().split()
    if len(keys) != 4:
        await message.answer("Ошибка! Требуется ровно 4 кода через пробел.")
        return
    await state.update_data(keys=[k.upper() for k in keys])
    await adm_safe_step3_ask_costs(message, state)


async def adm_safe_step3_ask_costs(event, state: FSMContext):
    await state.set_state(SafeConfigFull.choosing_costs_mode)
    buttons = [
        [InlineKeyboardButton(text="⚡ Авто-цены (100, 200, 350, 500 🍬)", callback_data="runes_costs_auto")],
        [InlineKeyboardButton(text="✍️ Свои цены свитков", callback_data="runes_costs_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    txt = "<b>Шаг 3: Стоимость свитков с подсказками к рунам</b>\n\nВыберите цены покупки за Candy Points:"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                                      parse_mode="HTML")
    else:
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(SafeConfigFull.choosing_costs_mode, F.data == "runes_costs_auto")
async def adm_safe_costs_auto(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    costs = [100, 200, 350, 500]
    await finalize_runes_full_setup(callback, state, data["names"], data["keys"], costs)


@dp.callback_query(SafeConfigFull.choosing_costs_mode, F.data == "runes_costs_custom")
async def adm_safe_costs_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SafeConfigFull.typing_custom_costs)
    await callback.message.edit_text(
        "<b>Шаг 3: Ввод цен свитков</b>\n\n"
        "Отправьте 4 целых числа стоимости через пробел:\n"
        "<i>Пример: 80 160 300 480</i>",
        parse_mode="HTML"
    )


@dp.message(SafeConfigFull.typing_custom_costs)
async def adm_safe_got_custom_costs(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        costs = [int(x) for x in message.text.strip().split()]
        if len(costs) != 4: raise ValueError
        data = await state.get_data()
        await finalize_runes_full_setup(message, state, data["names"], data["keys"], costs)
    except Exception:
        await message.answer("Ошибка! Введите ровно 4 целых положительных числа через пробел.")


async def finalize_runes_full_setup(event, state: FSMContext, names: list, keys: list, costs: list):
    hints = [
        (f"Тайна {names[0]}: Древний ключ — {keys[0]}", costs[0]),
        (f"Тайна {names[1]}: Древний ключ — {keys[1]}", costs[1]),
        (f"Тайна {names[2]}: Древний ключ — {keys[2]}", costs[2]),
        (f"Тайна {names[3]}: Древний ключ — {keys[3]}", costs[3])
    ]

    db.update_safe_runes_full(names, keys, hints)
    await state.clear()

    res = (
        "🔐 <b>РУНЫ СЕЙФА УСПЕШНО ОБНОВЛЕНЫ!</b>\n\n"
        f"1. {names[0]}: <code>{keys[0]}</code> ({costs[0]} 🍬)\n"
        f"2. {names[1]}: <code>{keys[1]}</code> ({costs[1]} 🍬)\n"
        f"3. {names[2]}: <code>{keys[2]}</code> ({costs[2]} 🍬)\n"
        f"4. {names[3]}: <code>{keys[3]}</code> ({costs[3]} 🍬)\n\n"
        "<i>Подсказки автоматически настроены под эти формулы. Подобрать их без покупки свитков невозможно!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« К панели", callback_data="adm_back")]])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(res, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(res, reply_markup=kb, parse_mode="HTML")


# --- Редактирование текстов постов ---

@dp.callback_query(F.data == "adm_edit_posts_menu")
async def adm_edit_posts_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить текст старта", callback_data="adm_edit_txt_start")],
        [InlineKeyboardButton(text="✏️ Изменить текст итогов", callback_data="adm_edit_txt_finish")],
        [InlineKeyboardButton(text="🔄 Сбросить тексты на дефолт", callback_data="adm_reset_txt_default")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="adm_back")]
    ]
    txt = (
        "📝 <b>РЕДАКТИРОВАНИЕ ТЕКСТОВ ПОСТОВ</b>\n\n"
        "Вы можете задать свои авторские тексты для публикаций в канале.\n\n"
        "<b>Доступные переменные для старта:</b>\n"
        "• {duration} — рассчитанное время (например: 'за 8 ч.')\n"
        "• {start_time} — время начала по МСК\n"
        "• {end_time} — время окончания по МСК\n"
        "• {bot_link} — юзернейм бота\n\n"
        "<b>Доступные переменные для итогов:</b>\n"
        "• {w1} — победитель рулетки\n"
        "• {w2} — топ сыщик архивов\n"
        "• {w3} — взломщик сейфа\n"
        "• {bot_link} — юзернейм бота"
    )
    await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.callback_query(F.data == "adm_edit_txt_start")
async def adm_edit_txt_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(PostEditing.editing_start_post)
    await callback.message.edit_text(
        "✏️ <b>Отправьте новый текст поста СТАРТА:</b>\n\n"
        "Используйте HTML-теги для оформления.\n"
        "Для отмены нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="adm_edit_posts_menu")]]),
        parse_mode="HTML"
    )


@dp.message(PostEditing.editing_start_post)
async def adm_save_start_txt(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    db.set_custom_post("start", message.text)
    await state.clear()
    await message.answer("✅ Шаблон поста старта успешно сохранен!")
    text, kb = get_admin_menu()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_edit_txt_finish")
async def adm_edit_txt_finish(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(PostEditing.editing_finish_post)
    await callback.message.edit_text(
        "✏️ <b>Отправьте новый текст поста ИТОГОВ:</b>\n\n"
        "Используйте HTML-теги для оформления.\n"
        "Для отмены нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="adm_edit_posts_menu")]]),
        parse_mode="HTML"
    )


@dp.message(PostEditing.editing_finish_post)
async def adm_save_finish_txt(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    db.set_custom_post("finish", message.text)
    await state.clear()
    await message.answer("✅ Шаблон поста итогов успешно сохранен!")
    text, kb = get_admin_menu()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_reset_txt_default")
async def adm_reset_txt_default(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    db.set_custom_post("start", None)
    db.set_custom_post("finish", None)
    await callback.answer("🔄 Все тексты сброшены на стандартные!", show_alert=True)
    await adm_edit_posts_menu(callback)


# --- Меню предпросмотров ---

@dp.callback_query(F.data == "adm_previews_menu")
async def adm_previews_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    buttons = [
        [InlineKeyboardButton(text="👁 Предпросмотр: Старт", callback_data="adm_preview_start")],
        [InlineKeyboardButton(text="👁 Предпросмотр: Итоги", callback_data="adm_preview_finish")],
        [InlineKeyboardButton(text="« Назад в панель", callback_data="adm_back")]
    ]
    await callback.message.edit_text("Выберите пост для предпросмотра перед публикацией в канал:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "adm_preview_start")
async def adm_prev_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    post_text = generate_start_post(cfg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Опубликовать этот пост в канал", callback_data="adm_send_post_start")],
        [InlineKeyboardButton(text="« Назад", callback_data="adm_previews_menu")]
    ])
    await callback.message.edit_text(f"👁 <b>ПРЕДПРОСМОТР ПОСТА СТАРТА:</b>\n\n{post_text}", reply_markup=kb,
                                     parse_mode="HTML")


@dp.callback_query(F.data == "adm_preview_finish")
async def adm_prev_finish(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    post_text = generate_finish_post(cfg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Опубликовать итоги в канал", callback_data="adm_send_post_finish")],
        [InlineKeyboardButton(text="« Назад", callback_data="adm_previews_menu")]
    ])
    await callback.message.edit_text(f"👁 <b>ПРЕДПРОСМОТР ПОСТА ИТОГОВ:</b>\n\n{post_text}", reply_markup=kb,
                                     parse_mode="HTML")


@dp.callback_query(F.data == "adm_send_post_start")
async def adm_send_start_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    post_text = generate_start_post(cfg)
    kb_post = get_channel_post_keyboard()
    ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
    await callback.answer(f"Результат: {msg}", show_alert=True)


@dp.callback_query(F.data == "adm_send_post_finish")
async def adm_send_finish_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    post_text = generate_finish_post(cfg)
    kb_post = get_channel_post_keyboard()
    ok, msg = await send_to_channel_safe(post_text, reply_markup=kb_post)
    await callback.answer(f"Результат: {msg}", show_alert=True)


@dp.callback_query(F.data == "adm_send_bear1_post")
async def adm_send_bear1_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    u1 = db.get_user(cfg["bear1_winner_id"]) if cfg["bear1_winner_id"] else None
    uname = u1['username'] if u1 else "Игрок"
    channel_post = (
        "🚨 <b>СЕКТОР «СЛАДОСТЬ» СОРВАН! МЕДВЕДЬ №1 НАШЕЛ ХОЗЯИНА!</b>\n\n"
        f"Счастливчик @{html.escape(uname)} только что прокрутил рулетку в боте и выбил <b>Хэллоуинского Медведя №1</b>!\n\n"
        "Рулетка закрыта навсегда. Но охота продолжается: впереди борьба за Медведя №2 (архивные коды) и Медведя №3 (взлом Сейфа)!"
    )
    kb_post = get_channel_post_keyboard()
    ok, msg = await send_to_channel_safe(channel_post, reply_markup=kb_post)
    await callback.answer(f"Результат: {msg}", show_alert=True)


# --- Настройка таймера МСК ---

@dp.callback_query(F.data == "adm_set_timer")
async def adm_set_timer(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminTimer.entering_dates)
    await callback.message.edit_text(
        "⏰ <b>НАСТРОЙКА РАСПИСАНИЯ ПО МСК</b>\n\n"
        "Отправьте диапазон сообщением в формате:\n"
        "ДД.ММ.ГГГГ ЧЧ:ММ - ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
        "<i>Пример: 31.10.2026 14:00 - 31.10.2026 22:30</i>\n"
        "<i>Длительность посчитается автоматически (например: за 8 ч. 30 мин.)!</i>\n\n"
        "Для сброса отправьте: clear",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.message(AdminTimer.entering_dates)
async def adm_save_timer(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    txt = message.text.strip()
    await state.clear()

    if txt.lower() == "clear":
        db.set_msk_dates(None, None)
        await message.answer("✅ МСК-таймер сброшен! Ивент управляется только вручную.")
    else:
        try:
            s_str, e_str = txt.split("-")
            db.set_msk_dates(s_str.strip(), e_str.strip())
            dur = calculate_duration_str(s_str.strip(), e_str.strip())
            await message.answer(f"✅ Расписание сохранено! Длительность ивента: <b>{dur}</b>.", parse_mode="HTML")
        except Exception:
            await message.answer("❌ Ошибка формата!")

    text, kb = get_admin_menu()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# --- Создание промокода ---

@dp.callback_query(F.data == "adm_create_code_flow")
async def adm_code_step1(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(CodeCreation.choosing_points)

    buttons = [
        [InlineKeyboardButton(text="50 🍬", callback_data="pts_preset_50"),
         InlineKeyboardButton(text="100 🍬", callback_data="pts_preset_100")],
        [InlineKeyboardButton(text="250 🍬", callback_data="pts_preset_250"),
         InlineKeyboardButton(text="500 🍬", callback_data="pts_preset_500")],
        [InlineKeyboardButton(text="✍️ Вписать своё число очков", callback_data="pts_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    await callback.message.edit_text("Шаг 1: Выберите или введите награду за промокод:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(CodeCreation.choosing_points, F.data.startswith("pts_preset_"))
async def adm_code_preset_pts(callback: CallbackQuery, state: FSMContext):
    pts = int(callback.data.split("_")[-1])
    await state.update_data(points=pts)
    await adm_code_step2_ask_type(callback, state)


@dp.callback_query(CodeCreation.choosing_points, F.data == "pts_custom")
async def adm_code_ask_custom_pts(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CodeCreation.typing_custom_points)
    await callback.message.edit_text("Отправьте число очков сообщением (например: 350):")


@dp.message(CodeCreation.typing_custom_points)
async def adm_code_got_custom_pts(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        pts = int(message.text.strip())
        await state.update_data(points=pts)
        await adm_code_step2_ask_type(message, state)
    except ValueError:
        await message.answer("Введите целое число!")


async def adm_code_step2_ask_type(event, state: FSMContext):
    await state.set_state(CodeCreation.choosing_code_type)
    buttons = [
        [InlineKeyboardButton(text="🎲 Сгенерировать рандомный", callback_data="code_type_rand")],
        [InlineKeyboardButton(text="✍️ Вписать свой код текстом", callback_data="code_type_custom")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_back")]
    ]
    txt = "Шаг 2: Способ формирования промокода:"
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(CodeCreation.choosing_code_type, F.data == "code_type_rand")
async def adm_code_finish_rand(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pts = data["points"]
    rand_hash = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    final_code = f"HEX-{rand_hash}"

    db.add_promo_code(final_code, pts)
    await state.clear()

    await callback.message.edit_text(
        f"✅ <b>Архивный код создан!</b>\n\n"
        f"Код: <code>{final_code}</code>\n"
        f"Награда: <b>{pts} 🍬</b>\n"
        f"Лимит: <b>1 активация</b>\n\n"
        f"<i>Прячьте этот код в старый пост канала!</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« К панели", callback_data="adm_back")]]),
        parse_mode="HTML"
    )


@dp.callback_query(CodeCreation.choosing_code_type, F.data == "code_type_custom")
async def adm_code_ask_custom_text(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CodeCreation.typing_custom_code)
    await callback.message.edit_text("Отправьте свой промокод (например: PUMPKIN_KING_2026):")


@dp.message(CodeCreation.typing_custom_code)
async def adm_code_finish_custom(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    pts = data["points"]
    code_text = message.text.strip().upper()

    db.add_promo_code(code_text, pts)
    await state.clear()

    await message.answer(
        f"✅ <b>Кастомный код сохранен!</b>\n\n"
        f"Код: <code>{html.escape(code_text)}</code>\n"
        f"Награда: <b>{pts} 🍬</b>\n"
        f"Лимит: <b>1 активация</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« К панели", callback_data="adm_back")]]
        ),
        parse_mode="HTML"
    )


# --- Список кодов и финал ---

@dp.callback_query(F.data == "adm_list_codes")
async def adm_list_codes(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    codes = db.get_all_codes()
    lines = ["📋 <b>АРХИВНЫЕ КОДЫ:</b>\n"]
    if not codes:
        lines.append("<i>Кодов нет.</i>")
    else:
        for c in codes[:15]:
            st = "❌ Использован" if c["is_used"] else "🟢 Активен"
            lines.append(f"• <code>{html.escape(c['code'])}</code> ({c['points']} 🍬) — {st}")
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Назад", callback_data="adm_back")]]), parse_mode="HTML")


@dp.callback_query(F.data == "adm_finish_results")
async def adm_finish_results(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    cfg = db.get_event_config()
    db.set_event_status(False)

    post_text = generate_finish_post(cfg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Опубликовать в канал прямо сейчас", callback_data="adm_send_post_finish")],
        [InlineKeyboardButton(text="« Назад в админку", callback_data="adm_back")]
    ])
    await callback.message.answer(f"🏁 <b>ИТОГИ ИВЕНТА СФОРМИРОВАНЫ:</b>\n\n{post_text}", reply_markup=kb,
                                  parse_mode="HTML")


@dp.callback_query(F.data == "adm_back")
async def adm_back_btn(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.clear()
    text, kb = get_admin_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# --- Точка входа ---

async def main():
    global BOT_USERNAME
    db.init_db()

    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    print(f"GiftEZ Halloween Bot запущен: @{BOT_USERNAME}")

    scheduler.add_job(check_msk_schedule, "interval", seconds=30)
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())