"""Run portable offline tests: python -m tests.run [media|api|integrations|events|preview|lifecycle|overlay|tracking|all]."""
import sys
import unittest


def main(group="all"):
    groups = {
        "media": ("tests.test_media",),
        "api": ("tests.test_api",),
        "integrations": ("tests.test_integrations",),
        "events": ("tests.test_clawbot_events",),
        "preview": ("tests.test_preview", "tests.test_remote_viewer",
                    "tests.test_remote_preview", "tests.test_remote_integration"),
        "remote": ("tests.test_remote_viewer", "tests.test_remote_preview",
                   "tests.test_remote_integration", "tests.test_remote_bridge"),
        "lifecycle": ("tests.test_lifecycle",),
        "local": ("tests.test_local_camera",),
        "overlay": ("tests.test_event_overlay", "tests.test_target_tracking"),
        "tracking": ("tests.test_target_tracking",),
        "all": ("tests.test_media", "tests.test_api", "tests.test_integrations",
                "tests.test_clawbot_events", "tests.test_preview", "tests.test_lifecycle",
                "tests.test_remote_viewer", "tests.test_remote_preview", "tests.test_remote_integration",
                "tests.test_remote_bridge", "tests.test_local_camera", "tests.test_event_overlay",
                "tests.test_target_tracking"),
    }
    if group not in groups:
        raise SystemExit("Choose: all, media, api, integrations, events, preview, lifecycle, overlay, tracking")
    suite = unittest.defaultTestLoader.loadTestsFromNames(groups[group])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
