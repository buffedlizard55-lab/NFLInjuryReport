"""Shared helpers for the test-suite."""

import json
import os

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


def fixture_json(name: str):
    return json.loads(fixture_text(name))
