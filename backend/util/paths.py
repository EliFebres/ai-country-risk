"""Where the project lives on disk.

One resolver, anchored on this file. Before v2.0 there were three, and they did
not agree:

* ``main.py`` walked up from the **current working directory** looking for a
  directory containing ``backend/``, and raised if it reached the filesystem
  root. Run from inside ``backend/`` it failed outright.
* ``prices_daemon.py`` did the same walk but fell back to ``__file__`` instead
  of raising.
* ``data_retrieval.py`` walked up from ``__file__`` looking for a directory
  *named* ``backend``.

So the parquet panel's writer resolved its path from the CWD and its reader
resolved the same path from the source tree. In an ordinary checkout they agree;
they are not obliged to. Anchoring everything on this file's location makes
them agree by construction, and makes the answer independent of where the
process was launched.
"""

from pathlib import Path

#: ``<repo>/backend/util`` - this file's own directory.
UTIL_DIR = Path(__file__).resolve().parent

#: ``<repo>/backend``
BACKEND_DIR = UTIL_DIR.parent

#: ``<repo>`` - the directory that must be on ``sys.path`` for ``backend.*``
#: imports to resolve, since the package has no ``__init__.py`` and relies on
#: PEP 420 namespace packages.
PROJECT_ROOT = BACKEND_DIR.parent

#: ``<repo>/backend/data`` - everything the backend keeps on disk outside the
#: database. Not created here; the writer creates it.
DATA_DIR = BACKEND_DIR / "data"

#: ``<repo>/backend/data/wb_panel_wide`` - the Hive-partitioned World Bank
#: panel, written by data_fetching and read back by the ETL.
PANEL_DIR = DATA_DIR / "wb_panel_wide"
