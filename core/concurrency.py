"""请求串行化与有界后台画像更新。"""
import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from core.protocols import JOB, LEASE, SERVICE

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


class ProfileUpdater:
    """固定 worker 数量；同一用户的待处理更新合并为最新会话。

    每个用户由 JOB 协议管理：idle → queued → running → idle；运行期间再提交
    进入 dirty，完成后处理最新会话。_pending 只保存负载，_jobs 保存控制状态。
    """
    def __init__(self, memory, workers=4, max_pending=256):
        self._memory = memory
        self._worker_count = max(1, workers)
        self._queue = asyncio.Queue(maxsize=max_pending)
        self._pending = {}
        self._jobs = {}
        self._tasks = []
        self.lifecycle = SERVICE.start()

    @property
    def _closed(self):
        return self.lifecycle.state in {"stopping", "closed"}

    def start(self):
        if self.lifecycle.state == "running":
            return
        self.lifecycle.send("start")
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self._worker_count)]
        if hasattr(self._memory, "flush_pending_archives"):
            self._tasks.append(asyncio.create_task(self._archive_loop()))

    def submit(self, user_id, conv_id):
        if self._closed:
            return False
        if user_id not in self._jobs:
            try:
                self._queue.put_nowait(user_id)
            except asyncio.QueueFull:
                logger.warning("画像更新队列已满，本次跳过")
                return False
            self._jobs[user_id] = JOB.start()
        self._jobs[user_id].send("submit")
        self._pending[user_id] = (conv_id, contextvars.copy_context())
        return True

    async def _archive_loop(self):
        while not self._closed:
            try:
                await self._memory.flush_pending_archives()
            except Exception:
                logger.warning("memory archive scan failed", exc_info=True)
            await asyncio.sleep(30)

    async def _worker(self):
        while not self._closed:
            user_id = await self._queue.get()
            try:
                while user_id in self._pending and not self._closed:
                    self._jobs[user_id].send("take")
                    conv_id, context = self._pending.pop(user_id)
                    try:
                        await asyncio.create_task(self._memory.maintain(user_id, conv_id), context=context)
                    except Exception as ex:
                        logger.warning("后台画像更新失败: %s", type(ex).__name__)
            finally:
                job = self._jobs.pop(user_id)
                job.send("cancel" if self._closed or asyncio.current_task().cancelling() else "finish")
                self._queue.task_done()

    async def close(self):
        if self.lifecycle.state == "closed":
            return
        if self.lifecycle.state == "running":
            self.lifecycle.send("stop")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._pending.clear()
        for job in self._jobs.values():
            job.send("cancel")
        self._jobs.clear()
        self._tasks.clear()
        self.lifecycle.send("close")
