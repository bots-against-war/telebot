import asyncio
import json
import logging

from aiohttp import web

from telebot import api, types
from telebot.bot_runner import BotRunner

logger = logging.getLogger(__name__)


def run_webhook_server(bot_runners: list[BotRunner], base_url: str, port: int):
    bot_runner_by_route = {bw.webhook_route(): bw for bw in bot_runners}
    logger.info("Running bots:\n" + "\n".join(f"/{path}: {bw.name}" for path, bw in bot_runner_by_route.items()))

    async def handle_update(request: web.Request):
        route = request.match_info.get("route")
        if route is None:
            return web.Response(status=404)
        bot_runner = bot_runner_by_route.get(route)
        if bot_runner is None:
            return web.Response(status=403)
        try:
            update = types.Update.de_json(await request.json())
            if update is not None:
                await bot_runner.bot.process_new_updates([update])
        except Exception as e:
            update_id = update.update_id if update is not None else "<not defined>"
            logger.exception(f"Unexpected error processing update #{update_id}:\n{update}")
        finally:
            return web.Response()

    async def setup_webhooks(_):
        for route, br in bot_runner_by_route.items():
            await br.bot.delete_webhook()
            await br.bot.set_webhook(url=f"{base_url}/{route}/")
            logger.info(f"Webhook set for {br.name}")

    async def run_background_tasks(_):
        loop = asyncio.get_event_loop()
        for bw in bot_runners:
            for idx, coro in enumerate(bw.background_jobs):
                loop.create_task(coro)
                logger.info(f"Background task #{idx} created for {bw.name}")

    async def close_client_session(_):
        await api.session_manager.close_session()

    app = web.Application()
    app.on_startup.append(setup_webhooks)
    app.on_startup.append(run_background_tasks)
    app.on_cleanup.append(close_client_session)
    app.router.add_post("/webhook/{route}/", handle_update)
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)
