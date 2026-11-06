"""支持 `PYTHONPATH=src python -m mapfetch` 直接运行（未安装时的等价方式）。"""

from .ui.cli import main

raise SystemExit(main())
