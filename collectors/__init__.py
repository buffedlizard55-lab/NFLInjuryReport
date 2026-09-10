"""NFL Injury Report collectors.

Standard-library only on purpose: the pipeline must run in a bare GitHub Actions
runner with no dependency install step, so nothing here imports requests/bs4/lxml.
"""

__version__ = "1.0.0"
