"""The executor registry: action_type -> callable, dispatched out-of-band.

An approved action still has to *run*, and running it is your application's
business logic: merge the PR, send the email, apply the change. You register one
executor per action_type; the registry looks it up, drives the
EXECUTING -> DONE/FAILED transitions around it, and stores the result.

Why the registry owns the transitions and not the executor: so a crashed
executor can never leave a row lying about its state. The registry marks
EXECUTING before the call and FAILED (with the traceback) on any exception, so
the queue always reflects reality: a half-finished action is visibly
half-finished, not silently still "approved."

What this cannot cover: a worker that dies outright (SIGKILL, OOM, power loss)
mid-executor. No Python code runs after that, so the row stays EXECUTING with
``executing_at`` set. That is deliberate: the side effect may or may not have
happened, and retrying blindly could do it twice. Find those rows with
``ActionApproval.objects.filter(status="executing", executing_at__lt=cutoff)``
and resolve them by hand.

Execution is expected to happen out-of-band (a Celery/RQ task, a worker thread)
so an HTTP approve returns immediately. This module does not choose your task
queue; it gives you ``execute(approval)`` to call from inside whatever you use.
"""
from __future__ import annotations

import logging
import traceback
from typing import Callable

logger = logging.getLogger(__name__)

# An executor takes the approval and returns a JSON-serializable result dict.
Executor = Callable[["object"], dict]


class ExecutorRegistry:
    """A mapping of action_type -> executor, with safe dispatch."""

    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, action_type: str, fn: Executor) -> None:
        if action_type in self._executors:
            raise ValueError(f"executor already registered for {action_type!r}")
        self._executors[action_type] = fn

    def executor(self, action_type: str) -> Callable[[Executor], Executor]:
        """Decorator form: ``@registry.executor('merge_pr')``."""
        def deco(fn: Executor) -> Executor:
            self.register(action_type, fn)
            return fn
        return deco

    def has(self, action_type: str) -> bool:
        return action_type in self._executors

    def execute(self, approval) -> dict:
        """Run the executor registered for ``approval.action_type``.

        Requires the approval to be APPROVED. The APPROVED -> EXECUTING step is
        an atomic claim: if another worker already claimed this row, this call
        raises ``InvalidTransition`` BEFORE the executor runs, so the action runs
        at most once. Drives EXECUTING -> DONE/FAILED and
        persists the result. NEVER raises out of the executor: an executor error
        becomes a FAILED row with the traceback in ``result``, because a raised
        exception in an out-of-band worker is invisible, but a FAILED row is not.
        Returns the result dict either way.
        """
        fn = self._executors.get(approval.action_type)
        if fn is None:
            # No executor is a configuration bug, but failing the ROW (not the
            # worker) keeps it visible in the queue instead of vanishing.
            approval.mark_executing()
            result = {"ok": False, "error": f"no executor for {approval.action_type!r}"}
            approval.mark_failed(result)
            return result

        approval.mark_executing()
        try:
            result = fn(approval) or {}
        except Exception as exc:  # noqa: BLE001 (an out-of-band raise is invisible)
            logger.exception("executor for %s failed (approval %s)",
                             approval.action_type, getattr(approval, "uuid", "?"))
            result = {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            approval.mark_failed(result)
            return result

        approval.mark_done(result)
        return result
