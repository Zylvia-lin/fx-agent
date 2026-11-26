"""Standard-library differences in Nuke's embedded Python 2.7 and Python 3."""

import errno
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

try:
    from contextlib import suppress
    from importlib import reload as reload_module
    text_type = str
except ImportError:
    reload_module = reload  # noqa: F821
    text_type = unicode  # noqa: F821

    @contextmanager
    def suppress(*exceptions):
        try:
            yield
        except exceptions:
            pass


def make_directory(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def remove_file(path):
    try:
        os.unlink(path)
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise


def reserve_file(path):
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))


def replace_file(source, destination):
    if hasattr(os, "replace"):
        os.replace(source, destination)
    elif os.name == "nt":
        import ctypes

        move = ctypes.windll.kernel32.MoveFileExW
        encoding = sys.getfilesystemencoding()
        source = source if isinstance(source, text_type) else source.decode(encoding)
        destination = destination if isinstance(destination, text_type) else destination.decode(encoding)
        if not move(source, destination, 1):  # MOVEFILE_REPLACE_EXISTING
            raise ctypes.WinError()
    else:
        os.rename(source, destination)


@contextmanager
def temporary_directory(prefix):
    name = tempfile.mkdtemp(prefix=prefix)
    try:
        yield name
    finally:
        shutil.rmtree(name)
