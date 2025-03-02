import asyncio
import contextlib
import functools
import logging
import signal
from types import FrameType
from typing import Awaitable, Callable, TypeVar
from weakref import WeakSet

logger = logging.getLogger(__name__)


class GracefulShutdownCondition:
    instances: WeakSet["GracefulShutdownCondition"] = WeakSet()

    def __init__(self, predicate: Callable[[], Awaitable[bool]], description: str) -> None:
        self.predicate = predicate
        self.description = description

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        cls.instances.add(instance)
        return instance

    async def is_ready(self) -> bool:
        return await self.predicate()


FunctionT = TypeVar("FunctionT", bound=Callable)


class PreventShutdown(GracefulShutdownCondition):
    """Shutdown condition for background jobs, for example:

    >>> async def background_job():
    ...     while True:
    ...         async with PreventShutdown("doing important stuff"):
    ...             # webhook app will not shut down while this context is active
    ...             await do_important_stuff()
    ...         await asyncio.sleep(10)

    Also allows preventing shutdown for the whole duration of a function and use a separate context
    for allowing shutdown.

    >>> prevent_shutdown = PreventShutdown("updating user info")
    ... @prevent_shutdown
    ... async def update_user_info():
    ...     while True:
    ...         users = await fetch_users()
    ...         updated_users = await process_users(users)
    ...         await update_users(updated_users)
    ...         async with prevent_shutdown.allow_shutdown():
    ...             await asyncio.sleep(120)


    """

    def __init__(self, reason: str):
        self._is_preventing_shutdown = False
        self._reason = reason
        super().__init__(
            predicate=self.is_ready_to_shutdown,
            description=f"Preventing shutdown, {self._reason}",
        )

    async def is_ready_to_shutdown(self) -> bool:
        return not self._is_preventing_shutdown

    def __call__(self, function: FunctionT) -> FunctionT:
        """Async function decorator, preventing shutdown for the duration of its execution"""

        @functools.wraps(function)
        async def decorated(*args, **kwargs):
            async with self:
                return await function(*args, **kwargs)

        return decorated  # type: ignore

    @contextlib.asynccontextmanager
    async def allow_shutdown(self):
        initial_state = self._is_preventing_shutdown
        logger.debug(f"Entering negated shutdown prevention context ({self._reason = }, {initial_state = })")
        self._is_preventing_shutdown = False
        yield
        logger.debug(f"Exiting negated shutdown prevention context ({self._reason = }, restoring {initial_state = })")
        self._is_preventing_shutdown = initial_state

    async def __aenter__(self):
        logger.debug(f"Entering shutdown prevention context ({self._reason = })")
        self._is_preventing_shutdown = True
        return None

    async def __aexit__(self, *args, **kwargs):
        logger.debug(f"Exiting shutdown prevention context ({self._reason = })")
        self._is_preventing_shutdown = False
        return None


class GracefulShutdownHandler:
    _singleton: "GracefulShutdownHandler | None" = None

    def __new__(cls, *args, **kwargs):
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __init__(self) -> None:
        self._is_shutting_down = False
        self._is_running = False

    def _shutdown_signal_handler(self, sig: int, frame: FrameType | None):
        if not self._is_shutting_down:
            logger.info(f"Shutdown signal received: {signal.Signals(sig).name}, entering shutdown state")
            self._is_shutting_down = True
        else:
            logger.info(f"Repeated shutdown signal received: {signal.Signals(sig).name}, ignoring")

    async def run(self):
        if self._is_running:
            raise RuntimeError("Graceful shutdown handler may be run only once")
        self._is_running = True
        signal.signal(signal.SIGINT, self._shutdown_signal_handler)
        signal.signal(signal.SIGTERM, self._shutdown_signal_handler)
        while True:
            await asyncio.sleep(0.5)
            if not self._is_shutting_down:
                continue

            for condition in GracefulShutdownCondition.instances:
                if not await condition.is_ready():
                    logger.info(f"Shutdown condition is not satisfied yet, waiting: {condition.description!r}")
                    break
            else:
                logger.info("All shutdown conditions are satisfied, shutting down! See ya.")
                raise SystemExit()


def is_shutting_down() -> bool:
    return GracefulShutdownHandler()._is_shutting_down
