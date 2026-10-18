# -*- coding: utf-8 -*-
"""Legacy entry point. Uses isolated temporary fixtures; never loads user data."""
from tests.run import main

if __name__ == "__main__":
    raise SystemExit(main("integrations"))
