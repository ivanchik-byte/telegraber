import asyncio
import logging
import signal
import sys

from app.config import settings
from app.userbot import userbot_service
from app.bot import init_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("telegraber")


async def main() -> None:
    settings.load_runtime_overrides()
    if not settings.BOT_TOKEN or not settings.OWNER_ID:
        logger.error("BOT_TOKEN and OWNER_ID are required in .env file.")
        sys.exit(1)

    logger.info("Starting Telegraber service...")

    try:
        userbot_ok = await userbot_service.start()
        if userbot_ok:
            me = userbot_service.me
            logger.info("Userbot started successfully as %s (ID: %s)", me.first_name, me.id)
        else:
            logger.info("No active session found. Awaiting onboarding via bot.")
    except Exception as exc:
        logger.warning("Could not auto-start userbot: %s", exc)

    bot = await init_bot()
    bot_me = await bot.get_me()
    logger.info("Bot controller is online as @%s (Owner ID: %s)", bot_me.username, settings.OWNER_ID)

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down clients...")
        await userbot_service.stop()
        if bot.is_connected():
            await bot.disconnect()
        logger.info("Telegraber stopped.")


if __name__ == "__main__":
    asyncio.run(main())
