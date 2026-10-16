"""Only one backend may own a local profile, including direct ASGI launches."""
from contextlib import contextmanager
import fcntl
import os

@contextmanager
def studio_writer_lock(root):
    descriptor = os.open(root / 'api.writer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 384)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another Studio backend already owns this local profile.') from error
        yield
    finally:
        os.close(descriptor)
