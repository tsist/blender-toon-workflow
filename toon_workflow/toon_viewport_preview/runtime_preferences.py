# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned export timeout and setup-error policy, without external render binaries."""
import time
import bpy

_watched={}


def configuration(scene):
    settings=getattr(scene,'toon_author_files',None)
    return dict(executable=bpy.app.binary_path,worker_timeout=settings.worker_timeout if settings else 300.,
        abort_on_error=settings.abort_on_error if settings else True)


def watch(scene,owner,submitted):
    timeout=configuration(scene)['worker_timeout']
    _watched[submitted['id']]=(owner,time.monotonic()+timeout)
    if not bpy.app.timers.is_registered(_tick):bpy.app.timers.register(_tick,first_interval=.25)
    return submitted


def _tick():
    for token,(owner,deadline) in list(_watched.items()):
        owner.poll();job=next((j for j in owner.jobs() if j['id']==token),None)
        if job is None or job['status']!='QUEUED':_watched.pop(token,None)
        elif time.monotonic()>=deadline:
            owner.cancel(token);_watched.pop(token,None)
    return .25 if _watched else None


def clear():
    _watched.clear()
    if bpy.app.timers.is_registered(_tick):bpy.app.timers.unregister(_tick)
