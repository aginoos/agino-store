# SPDX-License-Identifier: GPL-3.0-or-later
"""Server tests; make agino_store_server importable before any test module loads."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "server"))
