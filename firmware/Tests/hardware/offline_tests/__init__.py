"""
The framework's own tests. Fake transport only, never hardware.

These are the only files under Tests/hardware/ named test_*.py, and that
is deliberate: an editor, a hook or a pytest run that discovers this
tree must find only tests that cannot touch a device. Every context
built here is Mode.SELFTEST with fake_transport=True, which is the one
combination the framework accepts for a fake device - and which stamps
FRAMEWORK_SELFTEST on every result.

    python3 run_offline.py
"""
