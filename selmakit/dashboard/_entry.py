"""Streamlit script run by ``selmakit dashboard``.

Streamlit needs a file path to run, so this module exists purely to give the
console command something to point at. It renders the dashboard with the
:class:`~selmakit.dashboard.config.DashboardConfig` defaults — to customize
branding, write your own entry script (see the repo's ``dashboard.py``) and run
it with ``streamlit run``.
"""
from selmakit.dashboard import run

run()
