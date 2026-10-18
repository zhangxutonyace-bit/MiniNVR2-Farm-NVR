# -*- coding: utf-8 -*-
"""MiniNVR2: a local, lightweight multi-camera recorder.

Each camera normally has one preview stream shared by the web preview,
lightweight detection and timed JPEG capture. Event recording opens a separate
stream, preferring stream copy when the codec allows it. Completed event MP4
files are remuxed for browser playback. Resource use and recording durability
depend on the device, codec, operating system and storage.

Mail and the experimental iLink adapter are optional and disabled by default.
See README.md and docs/VALIDATION.md for the verified scope.
"""
__version__ = "2.2.2"
