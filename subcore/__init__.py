"""Internal package for sub-translator.

Everything the CLI needs lives here; `subtrans.py` is a thin entry point.
No third-party dependencies are used in `ui`, `config` or `checks` so that the
tool can always start up far enough to tell the user what is missing.
"""

from .config import VERSION

__all__ = ["VERSION"]
