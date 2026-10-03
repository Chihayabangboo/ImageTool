# -*- coding: utf-8 -*-
"""pytest 公共配置：把项目根目录加入 sys.path。

这样在 tests/ 目录里的测试可以直接 `import utils` / `import processor`，
即使用户在任意工作目录下执行 pytest 也不会因为找不到模块而报错。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
