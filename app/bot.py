import asyncio
import io
import time
from pathlib import Path
from typing import Optional, Dict, Any

import qrcode
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)

from app.config import settings
from app.session_manager import (
    save_session_string,
    delete_session,
    check_tdata_directory,
    get_default_device_profile,
)
from app.userbot import userbot_service, DownloadedMedia

bot_client: Optional[TelegramClient] = None
user_states: Dict[int, Dict[str, Any]] = {}


def is_owner(sender_id: int) -> bool:
    return sender_id == settings.OWNER_ID


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass


def cleanup_user_state(sender_id: int) -> None:
    state = user_states.pop(sender_id, None)
    if not state:
        return
    qr_task = state.get("qr_task")
    if qr_task and not qr_task.done():
        qr_task.cancel()
    client = state.get("client")
    if client:
        asyncio.create_task(_safe_disconnect(client))


def get_main_keyboard():
    if userbot_service.is_ready():
        return [
            [
                Button.inline("Параметры доставки", data=b"open_settings"),
                Button.inline("Статус", data=b"show_status"),
            ],
            [
                Button.inline("Сменить способ входа", data=b"show_login_methods"),
            ],
        ]
    return [
        [Button.inline("Вход по QR-коду (рекомендуется)", data=b"start_qr_login")],
        [Button.inline("Вход по номеру телефона", data=b"start_phone_login")],
        [Button.inline("Импорт Tdata из папки", data=b"trigger_tdata")],
    ]


def get_settings_keyboard():
    direct_mark = "[x] " if settings.FALLBACK_DESTINATION == "direct" else "[ ] "
    chan_mark = "[x] " if settings.FALLBACK_DESTINATION == "channel" else "[ ] "
    target_str = str(settings.TARGET_CHAT_ID) if settings.TARGET_CHAT_ID else "не задан"

    return [
        [
            Button.inline(f"{direct_mark}Личные сообщения", data=b"dest_direct"),
            Button.inline(f"{chan_mark}Канал / Группа", data=b"dest_channel"),
        ],
        [
            Button.inline(f"ID хранилища: {target_str}", data=b"prompt_target_chat"),
        ],
        [
            Button.inline("Вход по QR", data=b"start_qr_login"),
            Button.inline("Вход по телефону", data=b"start_phone_login"),
        ],
        [
            Button.inline("Импорт Tdata", data=b"trigger_tdata"),
            Button.inline("Сбросить сессию", data=b"confirm_reset"),
        ],
        [
            Button.inline("Закрыть меню", data=b"close_menu"),
        ],
    ]


def get_keypad(entered: str):
    display_code = " ".join(list(entered) + ["_"] * (5 - len(entered)))
    buttons = [
        [
            Button.inline("1", data=b"kp_1"),
            Button.inline("2", data=b"kp_2"),
            Button.inline("3", data=b"kp_3"),
        ],
        [
            Button.inline("4", data=b"kp_4"),
            Button.inline("5", data=b"kp_5"),
            Button.inline("6", data=b"kp_6"),
        ],
        [
            Button.inline("7", data=b"kp_7"),
            Button.inline("8", data=b"kp_8"),
            Button.inline("9", data=b"kp_9"),
        ],
        [
            Button.inline("⌫", data=b"kp_del"),
            Button.inline("0", data=b"kp_0"),
            Button.inline("Отмена", data=b"cancel_login"),
        ],
    ]
    text = (
        "**Код подтверждения отправлен в Telegram**\n\n"
        f"Код: `{display_code}`\n\n"
        "Нажимайте цифры на клавиатуре ниже.\n"
        "_(Не отправляйте код слитными цифрами в чат — Telegram сразу его аннулирует)_"
    )
    return text, buttons


async def execute_qr_login(chat_id: int):
    cleanup_user_state(chat_id)

    api_id, api_hash = settings.get_api_credentials()
    profile = get_default_device_profile()
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model=profile["device_model"],
        system_version=profile["system_version"],
        app_version=profile["app_version"],
        lang_code=profile["lang_code"],
        system_lang_code=profile["system_lang_code"],
    )
    await client.connect()

    try:
        qr_login = await client.qr_login()
    except Exception as exc:
        await _safe_disconnect(client)
        if bot_client:
            await bot_client.send_message(
                chat_id,
                f"Не удалось инициализировать вход по QR-коду: {exc}",
                buttons=get_main_keyboard(),
            )
        return

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(qr_login.url)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    bio.name = "qr_login.png"
    img.save(bio, "PNG")
    bio.seek(0)

    buttons = [
        [Button.inline("Обновить QR-код", data=b"refresh_qr")],
        [Button.inline("Отмена", data=b"cancel_login")],
    ]

    caption = (
        "**Вход по QR-коду**\n\n"
        "1. Откройте Telegram на телефоне\n"
        "2. Перейдите: **Настройки → Устройства → Подключить устройство**\n"
        "3. Наведите камеру на QR-код\n\n"
        "Код действителен 60 секунд."
    )

    msg = await bot_client.send_file(
        chat_id,
        bio,
        caption=caption,
        buttons=buttons,
    )

    async def _wait_qr():
        try:
            user = await qr_login.wait(timeout=60)
            session_str = StringSession.save(client.session)
            save_session_string(session_str)
            await client.disconnect()
            user_states.pop(chat_id, None)

            started = await userbot_service.start()
            if started:
                uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
                await bot_client.send_message(
                    chat_id,
                    f"**Авторизация успешна**\n\n"
                    f"Подключен аккаунт: **{user.first_name}** (`{uname}`).\n"
                    f"Бот готов к работе. Отправьте ссылку на пост для скачивания.",
                    buttons=get_main_keyboard(),
                )
        except asyncio.TimeoutError:
            try:
                await msg.edit(
                    "**Срок действия QR-кода истек**\n\n"
                    "Нажмите кнопку ниже, чтобы сгенерировать новый код.",
                    buttons=[
                        [Button.inline("Сгенерировать новый QR-код", data=b"refresh_qr")],
                        [Button.inline("Главное меню", data=b"cancel_login")],
                    ],
                )
            except Exception:
                pass
        except SessionPasswordNeededError:
            user_states[chat_id] = {
                "action": "await_2fa",
                "client": client,
            }
            await bot_client.send_message(
                chat_id,
                "**Двухфакторная аутентификация (2FA)**\n\n"
                "На аккаунте установлен облачный пароль. Введите пароль в ответ на это сообщение:",
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            user_states.pop(chat_id, None)
            await bot_client.send_message(
                chat_id,
                f"Ошибка авторизации: {e}",
                buttons=get_main_keyboard(),
            )
        finally:
            if not userbot_service.is_ready():
                await _safe_disconnect(client)

    qr_task = asyncio.create_task(_wait_qr())
    user_states[chat_id] = {
        "action": "await_qr",
        "client": client,
        "qr_task": qr_task,
        "msg_id": msg.id,
    }


async def handle_post_download(event: events.NewMessage.Event, link: str) -> None:
    if not userbot_service.is_ready():
        await event.reply(
            "Юзербот не авторизован. Выполните вход через /start перед скачиванием.",
            buttons=get_main_keyboard(),
        )
        return

    status_msg = await event.reply("Получение информации о публикации...")
    last_edit = 0.0

    def progress_callback(current: int, total: int, prefix: str):
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit > 2.5 and total > 0:
            last_edit = now
            pct = int((current / total) * 100)
            cur_mb = current / (1024 * 1024)
            tot_mb = total / (1024 * 1024)
            asyncio.create_task(
                status_msg.edit(f"{prefix}: {pct}% ({cur_mb:.1f}/{tot_mb:.1f} МБ)")
            )

    media_list: list[DownloadedMedia] = []
    try:
        media_list = await userbot_service.fetch_and_download(
            link, progress_callback=progress_callback
        )
        if not media_list:
            await status_msg.edit("В указанной публикации нет поддерживаемых медиафайлов.")
            return

        total_size = sum(item.size for item in media_list)
        total_size_mb = total_size / (1024 * 1024)
        has_heavy_file = any(item.size > 50 * 1024 * 1024 for item in media_list)
        count = len(media_list)

        sent_via_bot = False
        if not has_heavy_file and settings.FALLBACK_DESTINATION != "channel":
            await status_msg.edit(
                f"Отправка в диалог бота ({count} файл(ов), {total_size_mb:.1f} МБ)..."
            )
            try:
                for item in media_list:
                    await bot_client.send_file(
                        settings.OWNER_ID,
                        file=str(item.path),
                        caption=item.caption or None,
                    )
                sent_via_bot = True
                await status_msg.edit(
                    f"Доставлено в диалог бота: {count} файл(ов) ({total_size_mb:.1f} МБ)."
                )
            except Exception as exc:
                sent_via_bot = False
                await status_msg.edit(
                    f"Сбой отправки через Bot API ({exc}). "
                    f"Все файлы публикации будут отправлены через юзербота..."
                )

        if not sent_via_bot:
            if settings.FALLBACK_DESTINATION == "channel" and settings.TARGET_CHAT_ID:
                target_peer = settings.TARGET_CHAT_ID
                dest_title = f"канал-хранилище ({settings.TARGET_CHAT_ID})"
            else:
                target_peer = settings.OWNER_ID
                dest_title = "личные сообщения"

            await status_msg.edit(
                f"Отправка через юзербота в {dest_title} ({count} файл(ов), {total_size_mb:.1f} МБ)..."
            )
            await userbot_service.send_media_files(
                target_peer, media_list, progress_callback=progress_callback
            )
            await status_msg.edit(
                f"Все файлы публикации ({count} шт., {total_size_mb:.1f} МБ) доставлены в {dest_title}."
            )

    except PermissionError as e:
        await status_msg.edit(f"Ошибка доступа: {e}")
    except ValueError as e:
        await status_msg.edit(f"Ошибка: {e}")
    except Exception as e:
        await status_msg.edit(f"Ошибка обработки: {e}")
    finally:
        for item in media_list:
            try:
                if item.path.exists():
                    item.path.unlink()
                parent_dir = item.path.parent
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
            except Exception:
                pass


async def register_handlers(bot: TelegramClient) -> None:
    @bot.on(events.NewMessage(pattern=r"^/start$"))
    async def cmd_start(event: events.NewMessage.Event):
        if not is_owner(event.sender_id):
            return

        cleanup_user_state(event.sender_id)

        if userbot_service.is_ready():
            user = userbot_service.me
            uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
            mode_desc = (
                "Канал / Группа"
                if settings.FALLBACK_DESTINATION == "channel"
                else "Личные сообщения"
            )
            await event.reply(
                f"**Telegraber**\n\n"
                f"• Аккаунт: **{user.first_name}** (`{uname}`)\n"
                f"• Доставка файлов >50 МБ: **{mode_desc}**\n\n"
                f"Отправьте ссылку на публикацию Telegram для скачивания.\n"
                f"Управление параметрами: /settings",
                buttons=get_main_keyboard(),
            )
            return

        ok, msg, acc = await check_tdata_directory()
        if ok:
            await userbot_service.start()
            await event.reply(
                f"**Telegraber**\n\n"
                f"Сессия из Tdata успешно импортирована.\n"
                f"Аккаунт: **{acc.get('first_name')}** (+{acc.get('phone')}).\n\n"
                f"Отправьте ссылку на публикацию для загрузки.",
                buttons=get_main_keyboard(),
            )
            return
        elif msg == "2fa_required":
            user_states[event.sender_id] = {"action": "await_tdata_2fa"}
            await event.reply(
                "**Двухфакторная аутентификация (2FA)**\n\n"
                "Tdata защищена облачным паролем. Введите пароль 2FA в ответ на это сообщение:"
            )
            return

        await event.reply(
            "**Telegraber**\n\n"
            "Сервис готов к работе. Для скачивания защищенных публикаций "
            "необходимо авторизовать сессию юзербота.\n\n"
            "Выберите способ авторизации:",
            buttons=get_main_keyboard(),
        )

    @bot.on(events.NewMessage(pattern=r"^/settings$"))
    async def cmd_settings(event: events.NewMessage.Event):
        if not is_owner(event.sender_id):
            return
        await event.reply(
            "**Параметры доставки**\n\n"
            "• Файлы до 50 МБ отправляются напрямую в этот диалог.\n"
            "• Файлы более 50 МБ направляются в выбранное хранилище.\n"
            "• Если в альбоме есть файл >50 МБ, все файлы сообщения доставляются вместе.\n"
            "• Локальные файлы немедленно удаляются с сервера после отправки.",
            buttons=get_settings_keyboard(),
        )

    @bot.on(events.CallbackQuery)
    async def handle_callback(event: events.CallbackQuery.Event):
        if not is_owner(event.sender_id):
            await event.answer("Доступ запрещен", alert=True)
            return

        data = event.data.decode("utf-8")

        if data == "start_qr_login" or data == "refresh_qr":
            await event.answer("Генерация QR-кода...")
            await execute_qr_login(event.sender_id)
            return

        if data == "start_phone_login":
            cleanup_user_state(event.sender_id)
            user_states[event.sender_id] = {"action": "await_phone"}
            await event.respond(
                "**Вход по номеру телефона**\n\n"
                "Введите номер телефона в международном формате (например, `+79991234567`):"
            )
            await event.answer()
            return

        if data == "cancel_login" or data == "close_menu":
            cleanup_user_state(event.sender_id)
            await event.delete()
            return

        if data == "open_settings":
            await event.respond(
                "**Параметры доставки**",
                buttons=get_settings_keyboard(),
            )
            await event.answer()
            return

        if data == "show_status":
            if userbot_service.is_ready():
                user = userbot_service.me
                uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
                await event.respond(
                    f"**Статус системы**\n\n"
                    f"• Юзербот: активен\n"
                    f"• Профиль: {user.first_name} (`{uname}`)\n"
                    f"• Режим доставки: {settings.FALLBACK_DESTINATION}\n"
                    f"• Хранилище: {settings.TARGET_CHAT_ID or 'Личные сообщения'}"
                )
            else:
                await event.respond(
                    "**Статус системы**\n\n"
                    "Юзербот отключен. Нажмите /start для подключения.",
                    buttons=get_main_keyboard(),
                )
            await event.answer()
            return

        if data == "show_login_methods":
            await event.respond(
                "**Способы авторизации**\n\n"
                "Выберите способ подключения юзербота:",
                buttons=[
                    [Button.inline("Вход по QR-коду (рекомендуется)", data=b"start_qr_login")],
                    [Button.inline("Вход по номеру телефона", data=b"start_phone_login")],
                    [Button.inline("Импорт Tdata из папки", data=b"trigger_tdata")],
                    [Button.inline("Отмена", data=b"close_menu")],
                ],
            )
            await event.answer()
            return

        if data == "dest_direct":
            settings.FALLBACK_DESTINATION = "direct"
            settings.save_runtime_overrides()
            await event.edit(buttons=get_settings_keyboard())
            await event.answer("Режим: личные сообщения")
            return

        if data == "dest_channel":
            if not settings.TARGET_CHAT_ID:
                await event.answer("Сначала укажите ID канала/группы", alert=True)
                return
            settings.FALLBACK_DESTINATION = "channel"
            settings.save_runtime_overrides()
            await event.edit(buttons=get_settings_keyboard())
            await event.answer("Режим: канал-хранилище")
            return

        if data == "prompt_target_chat":
            user_states[event.sender_id] = {"action": "await_target_chat"}
            await event.respond(
                "Отправьте числовой ID канала или группы (например, `-1001234567890`).\n"
                "Убедитесь, что бот и юзербот состоят в этом чате."
            )
            await event.answer()
            return

        if data == "trigger_tdata":
            await event.answer("Проверка папки ./tdata_input...")
            ok, msg, acc = await check_tdata_directory()
            if ok:
                await userbot_service.start()
                await event.respond(
                    f"**Импорт Tdata успешен**\n\n"
                    f"Подключен аккаунт: **{acc.get('first_name')}** (+{acc.get('phone')}).\n"
                    f"Бот готов к работе.",
                    buttons=get_main_keyboard(),
                )
            elif msg == "2fa_required":
                user_states[event.sender_id] = {"action": "await_tdata_2fa"}
                await event.respond(
                    "**Двухфакторная аутентификация (2FA)**\n\n"
                    "Tdata защищена облачным паролем. Введите пароль в чат:"
                )
            else:
                await event.respond(
                    f"В папке ./tdata_input не найдено валидных данных Tdata ({msg}).\n"
                    "Поместите zip-архив или папку tdata в директорию `./tdata_input/`."
                )
            return

        if data == "confirm_reset":
            delete_session()
            await userbot_service.stop()
            cleanup_user_state(event.sender_id)
            await event.answer("Сессия удалена. Юзербот отключен.", alert=True)
            await event.respond(
                "Сессия сброшена. Для продолжения работы авторизуйтесь снова:",
                buttons=get_main_keyboard(),
            )
            return

        if data.startswith("kp_"):
            val = data[3:]
            state = user_states.get(event.sender_id, {})
            if state.get("action") != "await_code":
                await event.answer("Клавиатура ввода устарела.", alert=True)
                return

            entered = state.get("entered", "")
            if val == "del":
                entered = entered[:-1]
            elif val.isdigit() and len(entered) < 5:
                entered += val

            state["entered"] = entered

            if len(entered) < 5:
                text, buttons = get_keypad(entered)
                await event.edit(text, buttons=buttons)
                await event.answer()
                return

            await event.edit("Проверка кода подтверждения...")
            client: TelegramClient = state["client"]
            phone = state["phone"]
            phone_code_hash = state["phone_code_hash"]

            try:
                await client.sign_in(phone=phone, code=entered, phone_code_hash=phone_code_hash)
                session_str = StringSession.save(client.session)
                save_session_string(session_str)
                await client.disconnect()
                user_states.pop(event.sender_id, None)

                await userbot_service.start()
                user = userbot_service.me
                uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
                await event.edit(
                    f"**Авторизация успешна**\n\n"
                    f"Подключен аккаунт: **{user.first_name}** (`{uname}`).\n"
                    f"Бот готов к приему ссылок на публикации.",
                    buttons=get_main_keyboard(),
                )
            except SessionPasswordNeededError:
                user_states[event.sender_id] = {
                    "action": "await_2fa",
                    "client": client,
                }
                await event.edit(
                    "**Облачный пароль (2FA)**\n\n"
                    "На аккаунте включена двухфакторная аутентификация.\n"
                    "Отправьте пароль 2FA текстовым сообщением в чат:"
                )
            except PhoneCodeInvalidError:
                state["entered"] = ""
                text, buttons = get_keypad("")
                await event.edit(f"**Неверный код.** Попробуйте снова.\n\n{text}", buttons=buttons)
            except PhoneCodeExpiredError:
                cleanup_user_state(event.sender_id)
                await event.edit(
                    "**Код аннулирован сервером Telegram**\n\n"
                    "Telegram автоматически сжигает коды безопасности при обнаружении в переписке.\n"
                    "Рекомендуется войти по QR-коду — это мгновенно и без ввода цифр.",
                    buttons=[
                        [Button.inline("Вход по QR-коду (рекомендуется)", data=b"start_qr_login")],
                        [Button.inline("Попробовать снова", data=b"start_phone_login")],
                        [Button.inline("Главное меню", data=b"cancel_login")],
                    ],
                )
            except Exception as e:
                cleanup_user_state(event.sender_id)
                await event.edit(
                    f"**Ошибка авторизации:** {e}",
                    buttons=get_main_keyboard(),
                )
            await event.answer()
            return

    @bot.on(events.NewMessage)
    async def handle_text(event: events.NewMessage.Event):
        if not is_owner(event.sender_id) or not event.text:
            return

        text = event.text.strip()
        if text.startswith("/"):
            return

        state = user_states.get(event.sender_id, {})
        action = state.get("action")

        if action == "await_tdata_2fa":
            user_states.pop(event.sender_id, None)
            ok, msg, acc = await check_tdata_directory(password=text)
            if ok:
                await userbot_service.start()
                await event.reply(
                    f"**Пароль принят**\n\n"
                    f"Подключен аккаунт: **{acc.get('first_name')}** (+{acc.get('phone')}).\n"
                    f"Бот готов к работе.",
                    buttons=get_main_keyboard(),
                )
            else:
                await event.reply(
                    f"**Ошибка авторизации 2FA:** {msg}",
                    buttons=get_main_keyboard(),
                )
            return

        if action == "await_target_chat":
            user_states.pop(event.sender_id, None)
            try:
                chat_id = int(text)
                settings.TARGET_CHAT_ID = chat_id
                settings.save_runtime_overrides()
                await event.reply(
                    f"Целевое хранилище сохранено: `{chat_id}`",
                    buttons=get_settings_keyboard(),
                )
            except ValueError:
                await event.reply("ID должен быть целым числом (например, `-1001234567890`).")
            return

        if action == "await_phone":
            phone = text.replace(" ", "").replace("-", "")
            profile = get_default_device_profile()
            api_id, api_hash = settings.get_api_credentials()
            temp_client = TelegramClient(
                StringSession(),
                api_id,
                api_hash,
                device_model=profile["device_model"],
                system_version=profile["system_version"],
                app_version=profile["app_version"],
                lang_code=profile["lang_code"],
                system_lang_code=profile["system_lang_code"],
            )
            await temp_client.connect()
            try:
                res = await temp_client.send_code_request(phone)
                user_states[event.sender_id] = {
                    "action": "await_code",
                    "phone": phone,
                    "phone_code_hash": res.phone_code_hash,
                    "client": temp_client,
                    "entered": "",
                }
                kp_text, buttons = get_keypad("")
                await event.reply(kp_text, buttons=buttons)
            except Exception as e:
                await _safe_disconnect(temp_client)
                user_states.pop(event.sender_id, None)
                await event.reply(
                    f"**Не удалось запросить код:** {e}",
                    buttons=get_main_keyboard(),
                )
            return

        if action == "await_code":
            raw_digits = "".join(ch for ch in text if ch.isdigit())
            if len(raw_digits) == 5:
                client: TelegramClient = state.get("client")
                phone = state.get("phone")
                phone_code_hash = state.get("phone_code_hash")
                status_reply = await event.reply("Проверка кода...")
                try:
                    await client.sign_in(phone=phone, code=raw_digits, phone_code_hash=phone_code_hash)
                    session_str = StringSession.save(client.session)
                    save_session_string(session_str)
                    await client.disconnect()
                    user_states.pop(event.sender_id, None)

                    await userbot_service.start()
                    user = userbot_service.me
                    uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
                    await status_reply.edit(
                        f"**Авторизация успешна**\n\n"
                        f"Подключен аккаунт: **{user.first_name}** (`{uname}`).\n"
                        f"Бот готов к работе.",
                        buttons=get_main_keyboard(),
                    )
                except SessionPasswordNeededError:
                    user_states[event.sender_id] = {"action": "await_2fa", "client": client}
                    await status_reply.edit(
                        "**Облачный пароль (2FA)**\n\n"
                        "На аккаунте включен пароль двухфакторной аутентификации. Введите пароль в чат:"
                    )
                except PhoneCodeInvalidError:
                    await status_reply.edit("Неверный код. Попробуйте еще раз.")
                except PhoneCodeExpiredError:
                    cleanup_user_state(event.sender_id)
                    await status_reply.edit(
                        "**Код аннулирован сервером Telegram**\n\n"
                        "Telegram автоматически сжигает коды безопасности при отправке обычным текстом в чат.\n"
                        "Воспользуйтесь входом по QR-коду — это мгновенно и надежно.",
                        buttons=[
                            [Button.inline("Вход по QR-коду (рекомендуется)", data=b"start_qr_login")],
                            [Button.inline("Вход по номеру", data=b"start_phone_login")],
                        ],
                    )
                except Exception as e:
                    cleanup_user_state(event.sender_id)
                    await status_reply.edit(
                        f"Ошибка авторизации: {e}",
                        buttons=get_main_keyboard(),
                    )
            return

        if action == "await_2fa":
            client: TelegramClient = state.get("client")
            try:
                await client.sign_in(password=text)
                session_str = StringSession.save(client.session)
                save_session_string(session_str)
                await client.disconnect()
                user_states.pop(event.sender_id, None)

                await userbot_service.start()
                user = userbot_service.me
                uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
                await event.reply(
                    f"**Пароль подтвержден**\n\n"
                    f"Подключен аккаунт: **{user.first_name}** (`{uname}`).\n"
                    f"Бот готов к работе. Отправьте ссылку на публикацию.",
                    buttons=get_main_keyboard(),
                )
            except PasswordHashInvalidError:
                await event.reply("**Неверный пароль 2FA.** Попробуйте ввести еще раз:")
            except Exception as e:
                cleanup_user_state(event.sender_id)
                await event.reply(
                    f"Ошибка входа: {e}",
                    buttons=get_main_keyboard(),
                )
            return

        if "t.me/" in text:
            await handle_post_download(event, text)


async def init_bot() -> TelegramClient:
    global bot_client
    api_id, api_hash = settings.get_api_credentials()
    bot_client = TelegramClient(
        str(settings.SESSIONS_DIR / "bot_controller.session"),
        api_id=api_id,
        api_hash=api_hash,
    )
    await bot_client.start(bot_token=settings.BOT_TOKEN)
    await register_handlers(bot_client)
    return bot_client
