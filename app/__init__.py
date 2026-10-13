__version__ = "1.0.0"

import asyncio

_tasks = set()


def spawn(coro):
    """create_task guardando a referência (senão a tarefa pode sumir no meio)."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
