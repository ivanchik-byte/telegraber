import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Optional, Dict, Any

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
)

from app.config import settings
from app.session_manager import (
    get_saved_session_string,
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


def get_settings_keyboard():
    direct_mark = "✅ " if settings.FALLBACK_DESTINATION == "direct" else ""
    chan_mark = "✅ " if settings.FALLBACK_DESTINATION == "channel" else ""
    target_str = str(settings.TARGET_CHAT_ID) if settings.TARGET_CHAT_ID else "Не задан"

    return [
        [
            Button.inline(f"{direct_mark}ЛС / Избранное", data=b"dest_direct"),
            Button.inline(f"{chan_mark}Канал / Группа", data=b"dest_channel"),
        ],
        [
            Button.inline(f"🎯 ID хранилища: {target_str}", data=b"prompt_target_chat"),
        ],
        [
            Button.inline("🔄 Импорт из ./tdata_input", data=b"trigger_tdata"),
            Button.inline("🔑 Авторизация по номеру", data=b"start_phone_login"),
        ],
        [
            Button.inline("🗑 Сбросить сессию", data=b"confirm_reset"),
            Button.inline("✖ Закрыть", data=b"close_menu"),
        ],
    ]


async def handle_post_download(event: events.NewMessage.Event, link: str) -> None:
    if not userbot_service.is_ready():
        await event.reply("⚠️ Юзербот не авторизован. Нажмите /start для настройки.")
        return

    status_msg = await event.reply("🔎 Получаю информацию о посте...")
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
            await status_msg.edit("❌ В посте нет поддерживаемых медиафайлов.")
            return

        any_heavy = any(item.size > 50 * 1024 * 1024 for item in media_list)
        total_size_mb = sum(item.size for item in media_list) / (1024 * 1024)

        sent_via_bot = False
        # If any file exceeds 50MB, ALL files go to the fallback destination together
        if not any_heavy and settings.FALLBACK_DESTINATION != "channel":
            await status_msg.edit(
                f"📤 Отправляю {len(media_list)} файл(ов) в диалог бота ({total_size_mb:.1f} МБ)..."
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
                    f"✅ Успешно доставлено в диалог бота ({len(media_list)} файлов, {total_size_mb:.1f} МБ)"
                )
            except Exception as exc:
                sent_via_bot = False
                await status_msg.edit(
                    f"⚠️ Не удалось отправить через бота ({exc}). Перенаправляю ВСЕ файлы поста через юзербота..."
                )

        if not sent_via_bot:
            if settings.FALLBACK_DESTINATION == "channel" and settings.TARGET_CHAT_ID:
                target_peer = settings.TARGET_CHAT_ID
                dest_title = f"канал/группу ({settings.TARGET_CHAT_ID})"
            else:
                target_peer = settings.OWNER_ID
                dest_title = "личные сообщения / Избранное"

            await status_msg.edit(
                f"🚀 Юзербот отправляет {len(media_list)} файл(ов) в {dest_title} ({total_size_mb:.1f} МБ)..."
            )
            await userbot_service.send_media_files(
                target_peer, media_list, progress_callback=progress_callback
            )
            await status_msg.edit(
                f"✅ Все файлы поста ({len(media_list)} шт, {total_size_mb:.1f} МБ) успешно доставлены в {dest_title}!"
            )

    except PermissionError as e:
        await status_msg.edit(f"🔒 Ошибка доступа: {e}")
    except Exception as e:
        await status_msg.edit(f"❌ Ошибка загрузки: {e}")
    finally:
        # Atomic local cleanup: delete all temporary files immediately
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
    @bot.on(events.NewMessage(pattern="/start"))
    async def cmd_start(event: events.NewMessage.Event):
        if not is_owner(event.sender_id):
            return

        user_states.pop(event.sender_id, None)
        if userbot_service.is_ready():
            user = userbot_service.me
            uname = f"@{user.username}" if user.username else f"+{user.phone or user.id}"
            mode_desc = (
                "Канал/Группа"
                if settings.FALLBACK_DESTINATION == "channel"
                else "ЛС / Избранное"
            )
            await event.reply(
                f"👋 **Telegraber готов к работе!**\n\n"
                f"👤 Аккаунт юзербота: **{user.first_name}** ({uname})\n"
                f"📦 Доставка тяжелых файлов (>50 МБ): **{mode_desc}**\n\n"
                f"Отправьте ссылку на пост (публичный или закрытый) для скачивания медиа.\n"
                f"Для настроек используйте /settings.",
            )
            return

        # Check for tdata automatically
        ok, msg, acc = await check_tdata_directory()
        if ok:
            await userbot_service.start()
            await event.reply(
                f"🎉 **Обнаружена и успешно импортирована сессия из Tdata!**\n"
                f"Аккаунт: {acc.get('first_name')} (+{acc.get('phone')})\n"
                f"Теперь вы можете присылать ссылки на посты."
            )
            return
        elif msg == "2fa_required":
            user_states[event.sender_id] = {"action": "tdata_2fa"}
            await event.reply(
                "🔐 **Tdata защищена двухфакторным паролем (2FA).**\n\n"
                "Пожалуйста, введите пароль 2FA в ответ на это сообщение:"
            )
            return

        await event.reply(
            "⚠️ **Юзербот не подключен.**\n\n"
            "Выберите способ авторизации:\n"
            "1. Положите папку или `.zip` архив `tdata` в каталог `./tdata_input` и нажмите /start.\n"
            "2. Либо выполните вход по номеру телефона: /login",
            buttons=[
                [Button.inline("🔑 Войти по номеру телефона", data=b"start_phone_login")],
                [Button.inline("🔄 Проверить папку tdata", data=b"trigger_tdata")],
            ],
        )

    @bot.on(events.NewMessage(pattern="/settings"))
    async def cmd_settings(event: events.NewMessage.Event):
        if not is_owner(event.sender_id):
            return
        await event.reply(
            "⚙️ **Параметры доставки файлов:**\n\n"
            "• **Файлы до 50 МБ:** всегда отправляются прямо в диалог с ботом.\n"
            "• **Файлы более 50 МБ:**\n"
            "  — `ЛС / Избранное`: юзербот присылает файлы в личные сообщения.\n"
            "  — `Канал / Группа`: все файлы отправляются в указанный чат.\n\n"
            "• **Правило целостности:** если хотя бы один файл из альбома больше 50 МБ "
            "или не помещается в бота, **все** файлы поста отправляются вместе в выбранное хранилище.\n\n"
            "• **Очистка:** после отправки файлы сразу удаляются с диска сервера.",
            buttons=get_settings_keyboard(),
        )

    @bot.on(events.NewMessage(pattern="/login"))
    async def cmd_login(event: events.NewMessage.Event):
        if not is_owner(event.sender_id):
            return
        user_states[event.sender_id] = {"action": "await_phone"}
        await event.reply(
            "📱 **Вход по номеру телефона**\n\n"
            "Введите номер телефона аккаунта в международном формате (например, `+79991234567`):"
        )

    @bot.on(events.CallbackQuery)
    async def handle_callback(event: events.CallbackQuery.Event):
        if not is_owner(event.sender_id):
            await event.answer("Доступ запрещен", alert=True)
            return

        data = event.data.decode("utf-8")
        if data == "dest_direct":
            settings.FALLBACK_DESTINATION = "direct"
            settings.save_runtime_overrides()
            await event.edit(buttons=get_settings_keyboard())
            await event.answer("Режим: отправка в ЛС / Избранное")
        elif data == "dest_channel":
            if not settings.TARGET_CHAT_ID:
                await event.answer("Сначала укажите ID канала/группы ниже!", alert=True)
                return
            settings.FALLBACK_DESTINATION = "channel"
            settings.save_runtime_overrides()
            await event.edit(buttons=get_settings_keyboard())
            await event.answer("Режим: отправка в канал-хранилище")
        elif data == "prompt_target_chat":
            user_states[event.sender_id] = {"action": "await_target_chat"}
            await event.respond(
                "💬 Отправьте ID канала или группы-хранилища (например, `-1001234567890`).\n"
                "Убедитесь, что юзербот и бот добавлены в этот чат."
            )
            await event.answer()
        elif data == "trigger_tdata":
            await event.answer("Проверяю папку ./tdata_input...")
            ok, msg, acc = await check_tdata_directory()
            if ok:
                await userbot_service.start()
                await event.respond(
                    f"🎉 Импортирован аккаунт {acc.get('first_name')} (+{acc.get('phone')})"
                )
            elif msg == "2fa_required":
                user_states[event.sender_id] = {"action": "tdata_2fa"}
                await event.respond("🔐 Введите пароль 2FA для Tdata:")
            else:
                await event.respond(f"❌ Ошибка импорта: {msg}")
        elif data == "start_phone_login":
            user_states[event.sender_id] = {"action": "await_phone"}
            await event.respond("📱 Введите номер телефона аккаунта (например, `+79991234567`):")
            await event.answer()
        elif data == "confirm_reset":
            delete_session()
            await userbot_service.stop()
            await event.answer("Сессия удалена. Юзербот отключен.", alert=True)
            await event.edit(buttons=get_settings_keyboard())
        elif data == "close_menu":
            await event.delete()

    @bot.on(events.NewMessage)
    async def handle_text(event: events.NewMessage.Event):
        if not is_owner(event.sender_id) or not event.text:
            return

        text = event.text.strip()
        state = user_states.get(event.sender_id, {})
        action = state.get("action")

        # 1. State: 2FA for Tdata
        if action == "tdata_2fa":
            user_states.pop(event.sender_id, None)
            ok, msg, acc = await check_tdata_directory(password=text)
            if ok:
                await userbot_service.start()
                await event.reply(
                    f"🎉 Пароль 2FA принят! Аккаунт подключен: {acc.get('first_name')}"
                )
            else:
                await event.reply(f"❌ Ошибка авторизации: {msg}")
            return

        # 2. State: Target chat ID
        if action == "await_target_chat":
            user_states.pop(event.sender_id, None)
            try:
                chat_id = int(text)
                settings.TARGET_CHAT_ID = chat_id
                settings.save_runtime_overrides()
                await event.reply(
                    f"✅ Целевой чат сохранен: `{chat_id}`",
                    buttons=get_settings_keyboard(),
                )
            except ValueError:
                await event.reply("❌ ID должен быть целым числом (например, `-1001234567890`).")
            return

        # 3. State: Phone login step 1 (Phone number)
        if action == "await_phone":
            phone = text.replace(" ", "").replace("-", "")
            profile = get_default_device_profile()
            temp_client = TelegramClient(
                StringSession(),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
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
                }
                await event.reply(
                    "📩 Код подтверждения отправлен в Telegram.\n\n"
                    "Введите полученный код **через пробелы** (например: `1 2 3 4 5`):"
                )
            except Exception as e:
                await temp_client.disconnect()
                user_states.pop(event.sender_id, None)
                await event.reply(f"❌ Не удалось отправить код: {e}")
            return

        # 4. State: Phone login step 2 (Code)
        if action == "await_code":
            code = text.replace(" ", "").replace("-", "")
            client: TelegramClient = state.get("client")
            phone = state.get("phone")
            phone_code_hash = state.get("phone_code_hash")

            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                session_str = StringSession.save(client.session)
                save_session_string(session_str)
                await client.disconnect()
                user_states.pop(event.sender_id, None)

                await userbot_service.start()
                await event.reply("🎉 Авторизация успешна! Юзербот подключен.")
            except SessionPasswordNeededError:
                user_states[event.sender_id]["action"] = "await_login_2fa"
                await event.reply("🔐 На аккаунте включен пароль 2FA. Введите пароль:")
            except PhoneCodeInvalidError:
                await event.reply("❌ Неверный код. Попробуйте еще раз:")
            except Exception as e:
                await client.disconnect()
                user_states.pop(event.sender_id, None)
                await event.reply(f"❌ Ошибка входа: {e}")
            return

        # 5. State: Phone login step 3 (2FA password)
        if action == "await_login_2fa":
            client: TelegramClient = state.get("client")
            try:
                await client.sign_in(password=text)
                session_str = StringSession.save(client.session)
                save_session_string(session_str)
                await client.disconnect()
                user_states.pop(event.sender_id, None)

                await userbot_service.start()
                await event.reply("🎉 Пароль 2FA подтвержден! Юзербот подключен.")
            except PasswordHashInvalidError:
                await event.reply("❌ Неверный пароль 2FA. Попробуйте снова:")
            except Exception as e:
                await client.disconnect()
                user_states.pop(event.sender_id, None)
                await event.reply(f"❌ Ошибка входа: {e}")
            return

        # 6. Telegram link handler
        if "t.me/" in text:
            await handle_post_download(event, text)


async def init_bot() -> TelegramClient:
    global bot_client
    bot_client = TelegramClient(
        str(settings.SESSIONS_DIR / "bot_controller.session"),
        api_id=settings.TELEGRAM_API_ID,
        api_hash=settings.TELEGRAM_API_HASH,
    )
    await bot_client.start(bot_token=settings.BOT_TOKEN)
    await register_handlers(bot_client)
    return bot_client
