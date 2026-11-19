# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit candidate: one pure-data CPU worker, coalesced by live preview owner.

Only the owning main thread calls this manager. Worker operations must accept
(payload, checkpoint), must own their payload, and must never read bpy/RNA/GPU.
There is at most one submitted work item; pending revisions replace by owner.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import threading

class PhaseCancelled(RuntimeError):
    pass

class LatestPhaseJobs:
    def __init__(self):
        self._thread = threading.get_ident()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='toon-source-phase')
        self._serial = 0
        self._latest = {}
        self._pending = OrderedDict()
        self._results = {}
        self._active = None
        self._closed = False
        self._joined = False

    def _main(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('phase job manager must be used by its owning thread')

    @staticmethod
    def _run(operation, payload, stop):
        def checkpoint():
            if stop.is_set():
                raise PhaseCancelled('source phase revision is obsolete')
        checkpoint()
        value = operation(payload, checkpoint)
        checkpoint()
        return value

    def _pump(self):
        if self._active is not None:
            owner, ticket, stop, future = self._active
            if not future.done():
                return
            self._active = None
            # Retrieve every completion, but publish only its current ticket.
            try:
                value = future.result()
                disposition = (True, value)
            except Exception as error:
                disposition = (False, error)
            if self._latest.get(owner) == ticket:
                self._results[owner] = (ticket, disposition)
        if not self._closed and self._pending:
            owner, (ticket, operation, payload) = self._pending.popitem(last=False)
            stop = threading.Event()
            future = self._pool.submit(self._run, operation, payload, stop)
            self._active = (owner, ticket, stop, future)

    def request(self, owner, operation, payload):
        self._main()
        if self._closed:
            raise PhaseCancelled('source phase scheduler is closed')
        if not callable(operation):
            raise ValueError('pure-data phase operation must be callable')
        hash(owner)
        self._serial += 1
        ticket = self._serial
        self._latest[owner] = ticket
        self._results.pop(owner, None)
        self._pending[owner] = (ticket, operation, payload)
        if self._active is not None and self._active[0] == owner:
            self._active[2].set()
        self._pump()
        return ticket

    def poll(self, owner, ticket):
        """Return (ready,value); current worker failures propagate unchanged."""
        self._main()
        self._pump()
        if self._closed or self._latest.get(owner) != ticket:
            raise PhaseCancelled('source phase ticket is obsolete')
        row = self._results.get(owner)
        if row is None:
            return False, None
        success, value = row[1]
        if not success:
            raise value
        return True, value

    def cancel(self, owner):
        self._main()
        self._latest.pop(owner, None)
        self._pending.pop(owner, None)
        self._results.pop(owner, None)
        if self._active is not None and self._active[0] == owner:
            self._active[2].set()
        self._pump()

    def compact_state(self):
        self._main()
        self._pump()
        return dict(active=int(self._active is not None), pending_owners=len(self._pending),
                    retained_results=len(self._results), latest_owners=len(self._latest),
                    closed=self._closed, drained=self._joined)

    def close(self):
        self._main()
        if self._closed:
            return
        self._closed = True
        self._latest.clear(); self._pending.clear(); self._results.clear()
        if self._active is not None:
            self._active[2].set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def drain(self):
        """Join only after the cancelled pure-data operation has finished.

        Returns False while work is still running. A replacement scheduler
        must not be created until this returns True. No computation wait is
        performed here; the final public shutdown joins the idle worker.
        """
        self._main()
        if not self._closed:
            raise RuntimeError('close the phase scheduler before draining')
        self._pump()
        if self._active is not None:
            return False
        if not self._joined:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._joined = True
        return True

    def close_and_drain(self):
        """Cancel and join for load/unregister/exit, never for view navigation."""
        self._main()
        self.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._pump()
        self._joined = True
