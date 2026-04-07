from __future__ import annotations

from aiohttp import web


async def start_health_server(bot, port: int) -> web.AppRunner:
    async def handle_root(_: web.Request) -> web.Response:
        return web.Response(text="MotionXBot is running.\n", content_type="text/plain")

    async def handle_health(_: web.Request) -> web.Response:
        payload = {
            "ok": True,
            "discordReady": bot.is_ready(),
            "uptimeSeconds": int(bot.loop.time() - bot.started_at) if bot.started_at else 0,
        }
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server listening on port {port}")
    return runner
