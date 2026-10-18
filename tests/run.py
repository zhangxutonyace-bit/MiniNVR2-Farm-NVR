"""Run portable offline tests: python -m tests.run [media|api|integrations|events|lifecycle|all]."""
import sys
import unittest


def main(group="all"):
    groups = {
        "media": ("tests.test_media",),
        "api": ("tests.test_api",),
        "integrations": ("tests.test_integrations",),
        "events": ("tests.test_clawbot_events",),
        "lifecycle": ("tests.test_lifecycle",),
        "all": ("tests.test_media", "tests.test_api", "tests.test_integrations",
                "tests.test_clawbot_events", "tests.test_lifecycle"),
    }
    if group not in groups:
        raise SystemExit("Choose: all, media, api, integrations, events, lifecycle")
    suite = unittest.defaultTestLoader.loadTestsFromNames(groups[group])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
