"""Bounded REF/LDY recovery; successful responses are consumed by the caller."""

import math
import os
from dataclasses import asdict, dataclass

from .step00_collection_recovery import CollectionIncomplete, MAX_ITEM_ATTEMPTS, SUCCESS_STATES


@dataclass(frozen=True)
class RecoverySettings:
    single_gap: float = 3
    consecutive_failures: int = 2
    delays: tuple = (15, 30, 60)
    seconds: float = 180
    restarts: int = 1

    @classmethod
    def from_env(cls):
        prefix = "BESTBUY_DETAIL_RECOVERY_"
        settings = cls(
            single_gap=float(os.getenv(prefix + "SINGLE_GAP_SECONDS", "3")),
            consecutive_failures=int(os.getenv(prefix + "CONSECUTIVE_FAILURES", "2")),
            delays=tuple(float(v) for v in os.getenv(prefix + "DELAYS", "15,30,60").split(",")),
            seconds=float(os.getenv(prefix + "SECONDS", "180")),
            restarts=int(os.getenv(prefix + "MAX_RESTARTS", "1")),
        )
        if (not math.isfinite(settings.single_gap) or settings.single_gap < 0
                or not math.isfinite(settings.seconds) or settings.seconds <= 0
                or settings.consecutive_failures < 1 or settings.restarts not in (0, 1)
                or not settings.delays or any(not math.isfinite(v) or v < 0 for v in settings.delays)):
            raise ValueError("Invalid Best Buy detail recovery settings")
        return settings


class DetailTransportRecovery:
    """Charge only transport-outage time, with invocation-wide waits/restart limits."""

    def __init__(self, d, evidence, budget, states, execute, checkpoint, settings):
        self.d, self.evidence, self.states = d, evidence, states
        self.clock, self.sleep = budget.clock, budget.sleep
        self.execute, self.checkpoint, self.settings = execute, checkpoint, settings
        self.spent = 0
        self.active_since = None
        self.wait_index = self.restarts = self.streak = self.batch_failures = 0
        self.single_mode = False
        self.last_finished = None

    def remaining(self):
        active = 0 if self.active_since is None else self.clock() - self.active_since
        return max(0, self.settings.seconds - self.spent - active)

    def check_time(self):
        if self.remaining() <= 0:
            raise CollectionIncomplete("detail_transport_recovery_time_limit")

    def report(self):
        return dict(settings=asdict(self.settings),
                    recovery_seconds_used=round(self.settings.seconds - self.remaining(), 3),
                    recovery_attempts=self.wait_index, browser_restarts=self.restarts,
                    single_mode=self.single_mode, batch_failures=self.batch_failures,
                    consecutive_transport_failures=self.streak)

    def restore_mode(self, previous):
        # A new invocation gets a fresh bounded recovery allowance, but the same
        # verified run retains its learned batch/single mode across resume.
        self.single_mode = bool(previous.get("single_mode"))
        self.batch_failures = int(previous.get("batch_failures", 0))

    def pending(self, entry):
        _, sku, stage, _ = entry
        state = self.states[sku][stage]
        return state["status"] not in SUCCESS_STATES and state["attempt"] < MAX_ITEM_ATTEMPTS

    def pause(self, delay):
        if self.active_since is not None and delay >= self.remaining():
            raise CollectionIncomplete("detail_transport_recovery_time_limit")
        while delay > 0:
            step = min(30, delay)
            self.sleep(step)
            delay -= step
            self.check_time()

    def request(self, entries, *, single=False, probe=False):
        self.check_time()
        if single and self.last_finished is not None:
            self.pause(max(0, self.settings.single_gap - (self.clock() - self.last_finished)))
        started = self.clock()
        # Bootstrap and fetch use the same remaining budget while recovering.
        self.d.BROWSER_GRAPHQL_RECOVERY_REMAINING = self.remaining if self.active_since is not None else None
        try:
            ok = self.execute(entries, request_mode="probe" if probe else "single" if single else "batch")
        finally:
            self.last_finished = self.clock()
            self.d.BROWSER_GRAPHQL_RECOVERY_REMAINING = None
        if ok:
            self.streak = 0
            if self.active_since is not None:
                self.spent += self.clock() - self.active_since
                self.active_since = None
                self.evidence.emit("recovered", **self.report())
        else:
            if self.active_since is None:
                self.active_since = started
            if single:
                self.streak += 1
        self.checkpoint()
        self.check_time()
        return ok

    def recover(self, entry):
        target, sku, stage, _ = entry
        self.evidence.emit("circuit_open", sku_id=sku, operation=stage, **self.report())
        while True:
            self.check_time()
            if self.wait_index >= len(self.settings.delays):
                raise CollectionIncomplete("detail_transport_recovery_attempt_limit")
            delay = self.settings.delays[self.wait_index]
            self.wait_index += 1
            self.evidence.emit("waiting", wait_seconds=delay, recovery_attempt=self.wait_index,
                               sku_id=sku, operation=stage, reason="detail_transport_failure")
            self.checkpoint()
            self.pause(delay)
            # First probe keeps the session; the second recovery may restart it once.
            if self.wait_index >= 2 and self.restarts < self.settings.restarts:
                self.restarts += 1
                self.evidence.emit("browser_restart", restart_number=self.restarts,
                                   sku_id=sku, operation=stage)
                self.d.BROWSER_GRAPHQL_RECOVERY_REMAINING = self.remaining
                try:
                    self.d.recover_detail_browser_session(
                        self.d.target_url(target, sku), sku, fresh_profile=False,
                        require_pdp=not self.d.DETAIL_DIRECT_GRAPHQL)
                except Exception as exc:  # Browser launch/navigation backends use different exception types.
                    self.evidence.emit("browser_restart_failed", error=str(exc))
                    self.checkpoint()
                    continue
                finally:
                    self.d.BROWSER_GRAPHQL_RECOVERY_REMAINING = None
                self.check_time()
            if self.request([entry], single=True, probe=True):
                return

    def collect_single(self, entry):
        while self.pending(entry):
            if not self.request([entry], single=True) and self.streak >= self.settings.consecutive_failures:
                self.recover(entry)

    def collect(self, chunks):
        for entries in chunks:
            entries = [e for e in entries if self.pending(e)]
            if not entries:
                continue
            if not self.single_mode:
                if not self.request(entries):
                    self.batch_failures += 1
                    if self.batch_failures >= 2:
                        self.single_mode = True
                    self.evidence.emit("single_fallback", operation_count=len(entries),
                                       persistent=self.single_mode)
            # Includes only failed operations; verified empty/success is never retried.
            for entry in entries:
                self.collect_single(entry)
