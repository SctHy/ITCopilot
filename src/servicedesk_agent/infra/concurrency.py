"""请求串行化与跨进程租约续期。"""
import asyncio
import logging
from contextlib import asynccontextmanager
from servicedesk_agent.infra.protocols import LEASE

logger = logging.getLogger(__name__)


class ConversationBusyError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


async def run_leased(operation, renew, interval, *, lease=None):
    """Cancel local work when its renewable durable claim is lost.

    ``renew()`` returning False means the claim is gone. An exception (e.g. a transient
    database lock) is retried while the next attempt still lands before the lease
    (default ``3 * interval``) could expire; otherwise the work is cancelled before
    another worker can reclaim it. Cancellation cannot retract a remote side effect;
    callers still need idempotency keys and reconciliation. The database must fence
    final writes independently.
    """
    lease = 3 * interval if lease is None else lease
    loop = asyncio.get_running_loop()
    flow = LEASE.start()
    flow.send("acquired")
    task = asyncio.create_task(operation())

    async def heartbeat():
        renewed = loop.time()
        try:
            while True:
                await asyncio.sleep(max(.01, interval))
                started = loop.time()  # a successful renew sets lease_until >= started + lease
                try:
                    held = await asyncio.wait_for(renew(), max(.01, interval))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if loop.time() + interval < renewed + lease:
                        logger.warning("lease renewal failed; retrying before expiry", exc_info=True)
                        continue
                    raise
                if not held:
                    raise LeaseLostError("durable work lease lost")
                renewed = started
                flow.send("renew")
        except asyncio.CancelledError:
            raise
        except Exception:
            flow.send("lost")
            task.cancel()

    renewal = asyncio.create_task(heartbeat())
    try:
        return await task
    except asyncio.CancelledError:
        if flow.state == "lost" and not asyncio.current_task().cancelling():
            raise LeaseLostError("durable work lease lost") from None
        raise
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        flow.send("release")


class KeyedLocks:
    """同进程按键 FIFO 排队，无等待者时回收锁。"""
    def __init__(self):
        self._locks = {}

    @asynccontextmanager
    async def hold(self, key):
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


@asynccontextmanager
async def redis_lease(client, key, wait_s=30, lease_s=120):
    """跨进程互斥；续租失败取消持有者，避免失去锁后继续写入。"""
    lock = client.lock(key, timeout=lease_s, blocking_timeout=wait_s, thread_local=False)
    flow = LEASE.start()
    if not await lock.acquire():
        flow.send("timeout")
        raise ConversationBusyError("当前会话正在处理请求，请稍后重试")
    flow.send("acquired")
    owner = asyncio.current_task()
    lost = False

    async def renew():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(lease_s / 3)
                if not await lock.extend(lease_s, replace_ttl=True):
                    raise LeaseLostError("会话锁续租失败")
                flow.send("renew")
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            flow.send("lost")
            owner.cancel()

    renewal = asyncio.create_task(renew())
    try:
        yield
    except asyncio.CancelledError:
        if lost:
            raise LeaseLostError("会话锁已失效，本次请求已中止") from None
        raise
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        try:
            await lock.release()
        except Exception as ex:
            logger.warning("释放会话锁失败: %s", type(ex).__name__)
        flow.send("release")
