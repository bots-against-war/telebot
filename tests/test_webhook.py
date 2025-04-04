import asyncio
import gc
import signal
from uuid import uuid4

import aiohttp
import pytest

from telebot import AsyncTeleBot, types
from telebot.graceful_shutdown import GracefulShutdownCondition, PreventShutdown
from telebot.metrics import TelegramUpdateMetrics
from telebot.runner import BotRunner
from telebot.test_util import MockedAsyncTeleBot
from telebot.types import WebhookInfo
from telebot.types import service as service_types
from telebot.webhook import WebhookApp
from tests.utils import find_free_port

MOCK_TOKEN = uuid4().hex


COUNTED_MILLISECONDS = 0
RECEIVED_COMMANDS: list[types.Message] = []
RECEIVED_MESSAGES: list[types.Message] = []


def reset_global_test_state() -> None:
    """HACK HACK HACK"""
    global COUNTED_MILLISECONDS
    COUNTED_MILLISECONDS = 0
    RECEIVED_COMMANDS.clear()
    RECEIVED_MESSAGES.clear()


@pytest.fixture
def bot() -> MockedAsyncTeleBot:
    bot = MockedAsyncTeleBot(MOCK_TOKEN)

    @bot.message_handler(commands=["start", "help"])
    async def receive_cmd(m: types.Message):
        RECEIVED_COMMANDS.append(m)

    @bot.message_handler(commands=["error"])
    async def raise_error(m: types.Message):
        raise RuntimeError("AAA!!!")

    @bot.message_handler(commands=["handler_metrics"])
    async def save_data_to_handler_metrics(m: types.Message) -> service_types.HandlerResult:
        return service_types.HandlerResult(metrics={"hello": "world", "data": 1})

    @bot.message_handler()
    def receive_message(m: types.Message):  # bot converts all funcs to coroutine funcs on its own
        RECEIVED_MESSAGES.append(m)

    return bot


@pytest.fixture(
    params=[
        "testing-bot",
        "prefix/with-slashes",
        "префикс со странными символами 🎃🎃🎃",
        "unreasonably long prefix " * 100,
    ]
)
def bot_runner(bot: AsyncTeleBot, request: pytest.FixtureRequest) -> BotRunner:
    async def count_milliseconds():
        global COUNTED_MILLISECONDS
        while True:
            COUNTED_MILLISECONDS += 10
            await asyncio.sleep(0.01)

    return BotRunner(bot_prefix=request.param, bot=bot, background_jobs=[count_milliseconds()])


@pytest.mark.parametrize("webhook_already_exists", [True, False])
async def test_bot_runner(bot_runner: BotRunner, bot: MockedAsyncTeleBot, aiohttp_client, webhook_already_exists: bool):
    reset_global_test_state()
    subroute = bot_runner.webhook_subroute()
    route = "/webhook/" + subroute + "/"
    base_url = "http://127.0.0.1"
    metrics: list[TelegramUpdateMetrics] = []

    if webhook_already_exists:
        bot.add_return_values(
            "get_webhook_info", WebhookInfo(url=base_url + route, has_custom_certificate=False, pending_update_count=0)
        )

    async def metrics_handler(m: TelegramUpdateMetrics) -> None:
        metrics.append(m)

    webhook_app = WebhookApp(base_url, metrics_handler=metrics_handler)
    await webhook_app.add_bot_runner(bot_runner)
    client: aiohttp.ClientSession = await aiohttp_client(webhook_app.aiohttp_app)

    assert MOCK_TOKEN not in subroute

    assert len(bot.method_calls["get_webhook_info"]) == 1
    if not webhook_already_exists:
        assert len(bot.method_calls["set_webhook"]) == 1
        assert bot.method_calls["set_webhook"][0].kwargs == {"url": base_url + route}

    for i, text in enumerate(["текст сообщения", "/start", "/error", "еще текст", "/handler_metrics", "/help"]):
        resp = await client.post(
            route,
            json={
                "update_id": 10001110101 + i,
                "message": {
                    "message_id": 53 + i,
                    "from": {
                        "id": 1312,
                        "is_bot": False,
                        "first_name": "раз",
                        "last_name": "два",
                        "username": "testing",
                        "language_code": "en",
                    },
                    "chat": {
                        "id": 1312,
                        "first_name": "раз",
                        "last_name": "два",
                        "username": "testing",
                        "type": "private",
                    },
                    "date": 1653769757 + i,
                    "text": text,
                },
            },
        )
        assert resp.status == 200

    assert len(RECEIVED_MESSAGES) == 2
    assert [m.text for m in RECEIVED_MESSAGES] == ["текст сообщения", "еще текст"]

    assert len(RECEIVED_COMMANDS) == 2
    assert [m.text for m in RECEIVED_COMMANDS] == ["/start", "/help"]

    assert COUNTED_MILLISECONDS > 1, "Background job didn't count milliseconds!"

    assert len(metrics) == 6
    assert all(m["bot_prefix"] == bot_runner.bot_prefix for m in metrics)
    assert [m["update_id"] for m in metrics] == [
        10001110101,
        10001110102,
        10001110103,
        10001110104,
        10001110105,
        10001110106,
    ]
    assert [m["update_type"] for m in metrics] == ["message"] * 6
    assert [m["user_info"] for m in metrics] == [
        {
            "language_code": "en",
            "user_id_hash": "0f9684a825a9bb213bed2d01286cff30",
        }
    ] * 6
    assert [m["message_info"] for m in metrics] == [
        {
            "content_type": "text",
            "is_forwarded": False,
            "is_reply": False,
        }
    ] * 6
    assert [m["handler_name"] for m in metrics] == [
        "tests.test_webhook.bot.<locals>.receive_message",
        "tests.test_webhook.bot.<locals>.receive_cmd",
        "tests.test_webhook.bot.<locals>.raise_error",
        "tests.test_webhook.bot.<locals>.receive_message",
        "tests.test_webhook.bot.<locals>.save_data_to_handler_metrics",
        "tests.test_webhook.bot.<locals>.receive_cmd",
    ]
    assert [len(m["handler_test_durations"]) for m in metrics] == [4, 1, 2, 4, 3, 1]
    assert [m.get("exception_info") for m in metrics] == [
        None,
        None,
        {"type_name": "RuntimeError", "body": "AAA!!!"},
        None,
        None,
        None,
    ]
    assert all("processing_duration" in m for m in metrics)
    assert [m.get("handler_metrics") for m in metrics] == [None, None, None, None, {"data": 1, "hello": "world"}, None]


@pytest.mark.parametrize(
    "bot_name, token, expected_route_prefix",
    [
        pytest.param("hello-world", uuid4().hex, "hello-world"),
        pytest.param("hello world", uuid4().hex, "hello-world"),
        pytest.param(" Very Bad  Name For   a Bot!!!   ", uuid4().hex, "Very-Bad-Name-For-a-Bot!!!"),
        pytest.param("name/with/slashes", uuid4().hex, "name-with-slashes"),
        pytest.param("non-ASCII-✅", uuid4().hex, "non-ASCII-✅"),
    ],
)
def test_webhook_route_generation(bot_name: str, token: str, expected_route_prefix: str):
    bot = AsyncTeleBot(token)
    bot_runner = BotRunner(bot_prefix=bot_name, bot=bot)
    assert bot_runner.webhook_subroute().startswith(expected_route_prefix)


async def test_webhook_app_graceful_shutdown():
    # constructing bot object
    bot = MockedAsyncTeleBot("")
    message_processing_started = False
    message_processing_ended = False

    @bot.message_handler()
    async def time_consuming_message_processing(message: types.Message):
        nonlocal message_processing_started
        nonlocal message_processing_ended
        message_processing_started = True
        await asyncio.sleep(2)
        message_processing_ended = True

    # adding background task that prevents shutdown
    background_job_1_completed = False
    background_job_2_completed = False

    async def background_job_1():
        async with PreventShutdown("performing background task 1"):
            await asyncio.sleep(3)
            nonlocal background_job_1_completed
            background_job_1_completed = True

    prevent_shutdown = PreventShutdown("performing background task 2")

    @prevent_shutdown
    async def background_job_2():
        await asyncio.sleep(4)
        nonlocal background_job_2_completed
        background_job_2_completed = True
        async with prevent_shutdown.allow_shutdown():
            await asyncio.sleep(10)

    # constructing bot runner
    bot_runner = BotRunner("testing", bot, background_jobs=[background_job_1(), background_job_2()])
    subroute = bot_runner.webhook_subroute()
    base_url = "http://localhost"
    route = "/webhook/" + subroute + "/"
    webhook_app = WebhookApp(base_url)
    await webhook_app.add_bot_runner(bot_runner)

    # creating and running webhook app with system exit catching wrapper
    port = find_free_port()
    server_listening = asyncio.Future()
    server_exited_with_sys_exit = asyncio.Future()

    async def on_server_listening():
        server_listening.set_result(None)

    async def safe_run_webhook_app():
        try:
            await webhook_app.run(port, graceful_shutdown=True, on_server_listening=on_server_listening)
        except SystemExit:
            server_exited_with_sys_exit.set_result(None)

    asyncio.create_task(safe_run_webhook_app())
    await server_listening

    # validating setup sequence in bot
    # assert len(bot.method_calls["delete_webhook"]) == 1
    assert len(bot.method_calls["set_webhook"]) == 1
    assert bot.method_calls["set_webhook"][0].kwargs == {"url": base_url + route}

    MESSAGE_UPDATE_JSON = {
        "update_id": 10001110101,
        "message": {
            "message_id": 53,
            "from": {
                "id": 1312,
                "is_bot": False,
                "first_name": "раз",
                "last_name": "два",
                "username": "testing",
                "language_code": "en",
            },
            "chat": {
                "id": 1312,
                "first_name": "раз",
                "last_name": "два",
                "username": "testing",
                "type": "private",
            },
            "date": 1653769757,
            "text": "hello world",
        },
    }

    async def kill_bot_after(delay: float) -> None:
        await asyncio.sleep(delay)
        signal.raise_signal(signal.SIGTERM)

    async def send_message_update_after(delay: float) -> aiohttp.ClientResponse:
        await asyncio.sleep(delay)
        async with aiohttp.ClientSession(base_url=f"http://localhost:{port}") as session:
            return await session.post(route, json=MESSAGE_UPDATE_JSON)

    resp_completed, _, resp_rejected = await asyncio.gather(
        send_message_update_after(0),
        kill_bot_after(0.5),
        send_message_update_after(0.7),
    )

    await asyncio.wait_for(server_exited_with_sys_exit, timeout=30)

    assert resp_completed.status == 200
    assert resp_rejected.status == 500
    assert message_processing_started
    assert message_processing_ended
    assert background_job_1_completed
    assert background_job_2_completed


async def test_graceful_shutdown_conditions():
    GracefulShutdownCondition.instances.clear()

    for _ in range(1000):
        async with PreventShutdown("dummy"):
            a = 1 + 2
            a + 3

    actual_conditions = GracefulShutdownCondition.instances
    gc.collect()
    assert len(actual_conditions) == 0


async def test_webhook_app_background_tasks(bot_runner: BotRunner):
    reset_global_test_state()
    webhook_app = WebhookApp(base_url="localhost")
    await webhook_app.add_bot_runner(bot_runner)

    counted_milliseconds_when_bot_runner_removed = 0

    async def remove_bot_runner_after_delay() -> None:
        await asyncio.sleep(0.5)
        await webhook_app.remove_bot_runner(bot_runner)
        nonlocal counted_milliseconds_when_bot_runner_removed
        counted_milliseconds_when_bot_runner_removed = COUNTED_MILLISECONDS

    try:
        await asyncio.wait_for(
            asyncio.gather(
                webhook_app.run(port=12345, graceful_shutdown=False),
                remove_bot_runner_after_delay(),
            ),
            timeout=1,
        )
    except asyncio.TimeoutError:
        pass

    assert COUNTED_MILLISECONDS == counted_milliseconds_when_bot_runner_removed
