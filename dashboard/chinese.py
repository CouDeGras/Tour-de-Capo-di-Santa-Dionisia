"""Chinese script conversion for display, including permanently cached names."""
from functools import lru_cache
from opencc import OpenCC


@lru_cache(maxsize=1)
def _simplifier():
    return OpenCC("t2s")


def simplified(text):
    return _simplifier().convert(text)
