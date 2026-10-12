"""The app for tests that start uvicorn: built from the environment, then given Fernhill's team and calibrations.

A real deployment starts with neither (no team, nothing calibrated); these
tests stand in for a team that has set up and calibrated.
"""

import fernhill
from coach.api import build_services, create_app
from coach.config import Config

services = build_services(Config.from_env())
fernhill.seed(services.store)
app = create_app(services=services)
