import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # pcg_scatter_tool.py
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fake_unreal  # noqa: E402

# The backend imports `unreal` at module level; route it to the fake for the whole session.
sys.modules["unreal"] = fake_unreal
