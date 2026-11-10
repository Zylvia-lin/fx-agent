# -*- coding: utf-8 -*-
"""Maya executes module userSetup scripts in each GUI version (Python 2/3)."""
from fx_maya_bridge.startup import schedule as _fx_maya_schedule

_fx_maya_schedule()
