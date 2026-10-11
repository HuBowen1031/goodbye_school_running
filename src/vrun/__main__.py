"""``python -m vrun`` 入口。

真正的实现在 :mod:`vrun.cli` —— 这里只做转发，避免把参数解析写进包根。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
