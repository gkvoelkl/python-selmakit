"""selmakit dashboard — reference entry point.

Run with: uv run streamlit run dashboard.py

Customize an agent's dashboard by changing title, image and input_placeholder.
"""
from selmakit.dashboard import run

run(
    title="👩🏻 Selmakit Agent Dashboard",
    image="images/selma.png",
    input_placeholder="How can I help you today?",
)
