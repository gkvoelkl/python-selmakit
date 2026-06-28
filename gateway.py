"""selmakit gateway — reference entry point.

Builds the agent from .selmakit/selmakit.json with the default capabilities and
runs all channels, the worker, schedules and cron.

To add your own capability, append it via extra_capabilities:

    Gateway.from_config(extra_capabilities=[MyCapability(...)]).run()
"""
from dotenv import load_dotenv
load_dotenv()

from selmakit import Gateway


if __name__ == "__main__":
    Gateway.from_config().run()
