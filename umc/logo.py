"""Start-Logo."""
from . import VERSION
from .common import bold, cyan, dim, green

_TOP = ['               __     __', ' __ _____  ___/ /__ _/ /____ __ _  ___ ____  ___ ____ ____ ____', "/ // / _ \\/ _  / _ `/ __/ -_)  ' \\/ _ `/ _ \\/ _ `/ _ `/ -_) __/", '\\_,_/ .__/\\_,_/\\_,_/\\__/\\__/_/_/_/\\_,_/_//_/\\_,_/\\_, /\\__/_/', '   /_/                                          /___/']

_BOTTOM = ['      __           __          __', ' ____/ /  ___ ____/ /__ __ _  / /__', "/ __/ _ \\/ -_) __/  '_//  ' \\/  '_/", '\\__/_//_/\\__/\\__/_/\\_\\/_/_/_/_/\\_\\']

_LEFT = ["",
         "  [NOC] ══╦══▶ [site]",
         "          ╠══▶ [site]",
         "          ╚══▶ [site]"]


def banner():
    print()
    for line in _TOP:
        print(bold(cyan(line)))
    for i, line in enumerate(_BOTTOM):
        left = _LEFT[i] if i < len(_LEFT) else ""
        print(dim(left.ljust(27)) + bold(green(line)))
    print(dim(f"  Offline-Updates fuer Checkmk-Satelliten  ·  OS + Checkmk  ·  v{VERSION}"))
