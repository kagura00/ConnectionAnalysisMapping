from __future__ import annotations

import importlib

from pkg.base import Base, base_value


class Service(Base):
    def run(self, use_dynamic=False):
        result = base_value()
        if use_dynamic:
            importlib.import_module("pkg.base")
        return result

    def no_return(self):
        result = 1
        result += 1


def mixed(flag):
    if flag:
        return 1
    return None


def make_base() -> Base:
    return Base()


def use_factory():
    return make_base().base_method()


def use_constructor():
    return Base().base_method()


callback = lambda value: base_value()


def builtin_call(value):
    return len(str(value))
