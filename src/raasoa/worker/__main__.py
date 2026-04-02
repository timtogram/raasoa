"""Allow running the worker with `python -m raasoa.worker`."""

import asyncio

from raasoa.worker.batch import main

asyncio.run(main())
