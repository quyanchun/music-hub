#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
@author: HJK
@file: __init__.py
@time: 2019-01-26
"""

from .__version__ import __version__

def main(*args, **kwargs):
    # Servers use the API adapters without loading the interactive CLI/player.
    from .__main__ import main as command
    return command(*args, **kwargs)
