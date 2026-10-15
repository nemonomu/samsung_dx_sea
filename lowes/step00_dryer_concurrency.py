"""Cooperative browser requests: one Python thread, isolated per-product tasks.

Selenium commands and all output callbacks stay on the caller's thread. Only
fetch requests overlap in the browser. Nothing here reads settings or services.
"""
import asyncio
from contextlib import asynccontextmanager
import time
import uuid


START_FETCH = r"""
const [id, path, timeout] = arguments;
const requests = window.__lowesDryerRequests ||= Object.create(null);
const controller = new AbortController();
const entry = {controller, ready: false};
requests[id] = entry;
const timer = setTimeout(() => controller.abort(), timeout);
fetch(path, {credentials: 'same-origin', headers: {accept: 'application/json'},
             signal: controller.signal})
  .then(async r => {
    if (!r.ok) return {status: r.status, reason: 'http_error'};
    try { return {status: r.status, payload: await r.json()}; }
    catch (_) { return {status: r.status, reason: 'non_json_response'}; }
  })
  .catch(e => ({status: 'network', reason:
    e.name === 'AbortError' ? 'request_timeout' : 'network_error'}))
  .then(result => {entry.result = result; entry.ready = true;})
  .finally(() => clearTimeout(timer));
return true;
"""
POLL_FETCH = r"""
const requests = window.__lowesDryerRequests;
const entry = requests && requests[arguments[0]];
if (!entry) return {status: 'browser', reason: 'missing_browser_request'};
if (!entry.ready) return null;
delete requests[arguments[0]];
return entry.result;
"""
STOP_FETCH = r"""
const requests = window.__lowesDryerRequests;
const entry = requests && requests[arguments[0]];
if (entry) {entry.controller.abort(); delete requests[arguments[0]];}
"""


async def browser_fetch(driver, path, timeout):
    """Unique request handles; bounded polling; cancel on interruption as well."""
    request_id = uuid.uuid4().hex
    try:
        driver.execute_script(START_FETCH, request_id, path, timeout * 1000)
        deadline = time.monotonic() + timeout + 5
        while True:
            response = driver.execute_script(POLL_FETCH, request_id)
            if response is not None:
                return response if isinstance(response, dict) else {
                    "status": "browser", "reason": "invalid_browser_result"}
            if time.monotonic() >= deadline:
                return {"status": "network", "reason": "request_timeout"}
            await asyncio.sleep(0.05)
    except Exception as exc:
        return {"status": "browser", "reason": type(exc).__name__}
    finally:
        try:
            driver.execute_script(STOP_FETCH, request_id)
        except Exception:
            pass


class RegionGate:
    """Normal requests share a region; a store override drains them first."""
    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = 0
        self.waiting = 0
        self.owner = None

    @asynccontextmanager
    async def request(self):
        own = self.owner is asyncio.current_task()
        if not own:
            async with self.condition:
                await self.condition.wait_for(lambda: self.owner is None and not self.waiting)
                self.active += 1
        try:
            yield
        finally:
            if not own:
                async with self.condition:
                    self.active -= 1
                    self.condition.notify_all()

    @asynccontextmanager
    async def exclusive(self):
        async with self.condition:
            self.waiting += 1
            try:
                await self.condition.wait_for(lambda: self.owner is None and self.active == 0)
                self.owner = asyncio.current_task()
            finally:
                self.waiting -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.owner = None
                self.condition.notify_all()


def run_jobs(sources, limit, job, on_done, can_start=None, wait=0):
    """Refill available slots immediately; synchronously persist each completion.

can_start receives the number of in-flight products, allowing main selection to
reserve the last eligible slots without querying unselected products.
"""
    if limit not in (1, 2):
        raise ValueError("dryer concurrency must be 1 or 2")

    async def run():
        pending, iterator, exhausted = {}, iter(sources), False

        async def protected(source):
            # KeyboardInterrupt must not escape a child task into asyncio's loop.
            try:
                return await job(source), None
            except BaseException as exc:
                return None, exc

        async def process(source):
            try:
                result, error = await protected(source)
                if error is None:
                    # This has no await: journal/CSV/log writes cannot interleave.
                    on_done(source, result)
                    if wait:
                        await asyncio.sleep(wait)
                return error
            except BaseException as exc:
                return exc

        try:
            while pending or not exhausted:
                while not exhausted and len(pending) < limit and (
                        can_start is None or can_start(len(pending))):
                    try:
                        source = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    task = asyncio.create_task(process(source))
                    pending[task] = source
                if not pending:
                    break
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                errors = []
                for task in done:
                    pending.pop(task)
                    error = task.result()
                    if error is not None:
                        errors.append(error)
                if errors:
                    raise errors[0]
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())
