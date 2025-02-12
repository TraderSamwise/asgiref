import asyncio
import functools
import os
import sys
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor

from .current_thread_executor import CurrentThreadExecutor
from .local import Local

try:
    import contextvars  # Python 3.7+ only.
except ImportError:
    contextvars = None

import asyncio
import asyncio.coroutines
import contextvars
import functools
import inspect
import os
import sys
import threading
import warnings
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, overload

from .current_thread_executor import CurrentThreadExecutor
from .local import Local


def _restore_context(context):
    # Check for changes in contextvars, and set them to the current
    # context for downstream consumers
    for cvar in context:
        try:
            if cvar.get() != context.get(cvar):
                cvar.set(context.get(cvar))
        except LookupError:
            cvar.set(context.get(cvar))


def _iscoroutinefunction_or_partial(func: Any) -> bool:
    # Python < 3.8 does not correctly determine partially wrapped
    # coroutine functions are coroutine functions, hence the need for
    # this to exist. Code taken from CPython.
    if sys.version_info >= (3, 8):
        return asyncio.iscoroutinefunction(func)
    else:
        while inspect.ismethod(func):
            func = func.__func__
        while isinstance(func, functools.partial):
            func = func.func

        return asyncio.iscoroutinefunction(func)


class ThreadSensitiveContext:
    """Async context manager to manage context for thread sensitive mode
    This context manager controls which thread pool executor is used when in
    thread sensitive mode. By default, a single thread pool executor is shared
    within a process.
    In Python 3.7+, the ThreadSensitiveContext() context manager may be used to
    specify a thread pool per context.
    This context manager is re-entrant, so only the outer-most call to
    ThreadSensitiveContext will set the context.
    Usage:
    >>> import time
    >>> async with ThreadSensitiveContext():
    ...     await sync_to_async(time.sleep, 1)()
    """

    def __init__(self):
        self.token = None

    async def __aenter__(self):
        try:
            SyncToAsync.thread_sensitive_context.get()
        except LookupError:
            self.token = SyncToAsync.thread_sensitive_context.set(self)

        return self

    async def __aexit__(self, exc, value, tb):
        if not self.token:
            return

        executor = SyncToAsync.context_to_thread_executor.pop(self, None)
        if executor:
            executor.shutdown()
        SyncToAsync.thread_sensitive_context.reset(self.token)


class AsyncToSync:
    """
    Utility class which turns an awaitable that only works on the thread with
    the event loop into a synchronous callable that works in a subthread.

    If the call stack contains an async loop, the code runs there.
    Otherwise, the code runs in a new loop in a new thread.

    Either way, this thread then pauses and waits to run any thread_sensitive
    code called from further down the call stack using SyncToAsync, before
    finally exiting once the async task returns.
    """

    # Maps launched Tasks to the threads that launched them (for locals impl)
    launch_map = {}

    # Keeps track of which CurrentThreadExecutor to use. This uses an asgiref
    # Local, not a threadlocal, so that tasks can work out what their parent used.
    executors = Local()

    # Single-thread executor for thread-sensitive code
    single_thread_executor = ThreadPoolExecutor(max_workers=1)

    # Maintain a contextvar for the current execution context. Optionally used
    # for thread sensitive mode.
    thread_sensitive_context: "contextvars.ContextVar[str]" = contextvars.ContextVar(
        "thread_sensitive_context"
    )

    # Contextvar that is used to detect if the single thread executor
    # would be awaited on while already being used in the same context
    deadlock_context: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
        "deadlock_context"
    )

    # Maintaining a weak reference to the context ensures that thread pools are
    # erased once the context goes out of scope. This terminates the thread pool.
    context_to_thread_executor: "weakref.WeakKeyDictionary[object, ThreadPoolExecutor]" = (
        weakref.WeakKeyDictionary()
    )

    def __init__(self, awaitable, force_new_loop=False):
        self.awaitable = awaitable
        if force_new_loop:
            # They have asked that we always run in a new sub-loop.
            self.main_event_loop = None
        else:
            try:
                self.main_event_loop = asyncio.get_event_loop()
            except RuntimeError:
                # There's no event loop in this thread. Look for the threadlocal if
                # we're inside SyncToAsync
                self.main_event_loop = getattr(
                    SyncToAsync.threadlocal, "main_event_loop", None
                )

    def __call__(self, *args, **kwargs):
        # You can't call AsyncToSync from a thread with a running event loop
        try:
            event_loop = asyncio.get_event_loop()
        except RuntimeError:
            pass
        else:
            if event_loop.is_running():
                raise RuntimeError(
                    "You cannot use AsyncToSync in the same thread as an async event loop - "
                    "just await the async function directly."
                )
        # Make a future for the return information
        call_result = Future()
        # Get the source thread
        source_thread = threading.current_thread()
        # Make a CurrentThreadExecutor we'll use to idle in this thread - we
        # need one for every sync frame, even if there's one above us in the
        # same thread.
        if hasattr(self.executors, "current"):
            old_current_executor = self.executors.current
        else:
            old_current_executor = None
        current_executor = CurrentThreadExecutor()
        self.executors.current = current_executor
        # Use call_soon_threadsafe to schedule a synchronous callback on the
        # main event loop's thread if it's there, otherwise make a new loop
        # in this thread.
        try:
            if not (self.main_event_loop and self.main_event_loop.is_running()):
                # Make our own event loop - in a new thread - and run inside that.
                loop = asyncio.new_event_loop()
                loop_executor = ThreadPoolExecutor(max_workers=1)
                loop_future = loop_executor.submit(
                    self._run_event_loop,
                    loop,
                    self.main_wrap(
                        args, kwargs, call_result, source_thread, sys.exc_info()
                    ),
                )
                if current_executor:
                    # Run the CurrentThreadExecutor until the future is done
                    current_executor.run_until_future(loop_future)
                # Wait for future and/or allow for exception propagation
                loop_future.result()
            else:
                # Call it inside the existing loop
                self.main_event_loop.call_soon_threadsafe(
                    self.main_event_loop.create_task,
                    self.main_wrap(
                        args, kwargs, call_result, source_thread, sys.exc_info()
                    ),
                )
                if current_executor:
                    # Run the CurrentThreadExecutor until the future is done
                    current_executor.run_until_future(call_result)
        finally:
            # Clean up any executor we were running
            if hasattr(self.executors, "current"):
                del self.executors.current
            if old_current_executor:
                self.executors.current = old_current_executor
        # Wait for results from the future.
        return call_result.result()

    def _run_event_loop(self, loop, coro):
        """
        Runs the given event loop (designed to be called in a thread).
        """
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        finally:
            try:
                if hasattr(loop, "shutdown_asyncgens"):
                    loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                asyncio.set_event_loop(self.main_event_loop)

    def __get__(self, parent, objtype):
        """
        Include self for methods
        """
        func = functools.partial(self.__call__, parent)
        return functools.update_wrapper(func, self.awaitable)

    async def main_wrap(self, args, kwargs, call_result, source_thread, exc_info):
        """
        Wraps the awaitable with something that puts the result into the
        result/exception future.
        """
        current_task = SyncToAsync.get_current_task()
        self.launch_map[current_task] = source_thread
        try:
            # If we have an exception, run the function inside the except block
            # after raising it so exc_info is correctly populated.
            if exc_info[1]:
                try:
                    raise exc_info[1]
                except:
                    result = await self.awaitable(*args, **kwargs)
            else:
                result = await self.awaitable(*args, **kwargs)
        except Exception as e:
            call_result.set_exception(e)
        else:
            call_result.set_result(result)
        finally:
            del self.launch_map[current_task]


class SyncToAsync:
    """
    Utility class which turns a synchronous callable into an awaitable that
    runs in a threadpool. It also sets a threadlocal inside the thread so
    calls to AsyncToSync can escape it.

    If thread_sensitive is passed, the code will run in the same thread as any
    outer code. This is needed for underlying Python code that is not
    threadsafe (for example, code which handles SQLite database connections).

    If the outermost program is async (i.e. SyncToAsync is outermost), then
    this will be a dedicated single sub-thread that all sync code runs in,
    one after the other. If the outermost program is sync (i.e. AsyncToSync is
    outermost), this will just be the main thread. This is achieved by idling
    with a CurrentThreadExecutor while AsyncToSync is blocking its sync parent,
    rather than just blocking.
    """

    # If they've set ASGI_THREADS, update the default asyncio executor for now
    if "ASGI_THREADS" in os.environ:
        loop = asyncio.get_event_loop()
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=int(os.environ["ASGI_THREADS"]))
        )

    # Maps launched threads to the coroutines that spawned them
    launch_map = {}

    # Storage for main event loop references
    threadlocal = threading.local()

    # Single-thread executor for thread-sensitive code
    single_thread_executor = ThreadPoolExecutor(max_workers=1)

    # Maintain a contextvar for the current execution context. Optionally used
    # for thread sensitive mode.
    thread_sensitive_context: "contextvars.ContextVar[ThreadSensitiveContext]" = (
        contextvars.ContextVar("thread_sensitive_context")
    )

    # Contextvar that is used to detect if the single thread executor
    # would be awaited on while already being used in the same context
    deadlock_context: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
        "deadlock_context"
    )

    # Maintaining a weak reference to the context ensures that thread pools are
    # erased once the context goes out of scope. This terminates the thread pool.
    context_to_thread_executor: "weakref.WeakKeyDictionary[ThreadSensitiveContext, ThreadPoolExecutor]" = (
        weakref.WeakKeyDictionary()
    )

    def __init__(self, func, thread_sensitive=False):
        self.func = func
        self._thread_sensitive = thread_sensitive

    async def __call__(self, *args, **kwargs):
        loop = asyncio.get_event_loop()

        # Work out what thread to run the code in
        if self._thread_sensitive:
            if hasattr(AsyncToSync.executors, "current"):
                # If we have a parent sync thread above somewhere, use that
                executor = AsyncToSync.executors.current
            else:
                # Otherwise, we run it in a fixed single thread
                executor = self.single_thread_executor
        else:
            executor = None  # Use default

        if contextvars is not None:
            context = contextvars.copy_context()
            child = functools.partial(self.func, *args, **kwargs)
            func = context.run
            args = (child,)
            kwargs = {}
        else:
            func = self.func

        # Run the code in the right thread
        future = loop.run_in_executor(
            executor,
            functools.partial(
                self.thread_handler,
                loop,
                self.get_current_task(),
                sys.exc_info(),
                func,
                *args,
                **kwargs
            ),
        )
        return await asyncio.wait_for(future, timeout=None)

    def __get__(self, parent, objtype):
        """
        Include self for methods
        """
        return functools.partial(self.__call__, parent)

    def thread_handler(self, loop, source_task, exc_info, func, *args, **kwargs):
        """
        Wraps the sync application with exception handling.
        """
        # Set the threadlocal for AsyncToSync
        self.threadlocal.main_event_loop = loop
        # Set the task mapping (used for the locals module)
        current_thread = threading.current_thread()
        if AsyncToSync.launch_map.get(source_task) == current_thread:
            # Our parent task was launched from this same thread, so don't make
            # a launch map entry - let it shortcut over us! (and stop infinite loops)
            parent_set = False
        else:
            self.launch_map[current_thread] = source_task
            parent_set = True
        # Run the function
        try:
            # If we have an exception, run the function inside the except block
            # after raising it so exc_info is correctly populated.
            if exc_info[1]:
                try:
                    raise exc_info[1]
                except:
                    return func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        finally:
            # Only delete the launch_map parent if we set it, otherwise it is
            # from someone else.
            if parent_set:
                del self.launch_map[current_thread]

    @staticmethod
    def get_current_task():
        """
        Cross-version implementation of asyncio.current_task()

        Returns None if there is no task.
        """
        try:
            if hasattr(asyncio, "current_task"):
                # Python 3.7 and up
                return asyncio.current_task()
            else:
                # Python 3.6
                return asyncio.Task.current_task()
        except RuntimeError:
            return None


# Lowercase is more sensible for most things
sync_to_async = SyncToAsync
async_to_sync = AsyncToSync

_F = Any


# Python 3.12 deprecates asyncio.iscoroutinefunction() as an alias for
# inspect.iscoroutinefunction(), whilst also removing the _is_coroutine marker.
# The latter is replaced with the inspect.markcoroutinefunction decorator.
# Until 3.12 is the minimum supported Python version, provide a shim.
# Django 4.0 only supports 3.8+, so don't concern with the _or_partial backport.

if hasattr(inspect, "markcoroutinefunction"):
    iscoroutinefunction = inspect.iscoroutinefunction
    markcoroutinefunction: Callable[[_F], _F] = inspect.markcoroutinefunction
else:
    iscoroutinefunction = asyncio.iscoroutinefunction  # type: ignore[assignment]

    def markcoroutinefunction(func: _F) -> _F:
        func._is_coroutine = asyncio.coroutines._is_coroutine  # type: ignore
        return func


if sys.version_info >= (3, 8):
    _iscoroutinefunction_or_partial = iscoroutinefunction
else:

    def _iscoroutinefunction_or_partial(func: Any) -> bool:
        # Python < 3.8 does not correctly determine partially wrapped
        # coroutine functions are coroutine functions, hence the need for
        # this to exist. Code taken from CPython.
        while inspect.ismethod(func):
            func = func.__func__
        while isinstance(func, functools.partial):
            func = func.func

        return iscoroutinefunction(func)
