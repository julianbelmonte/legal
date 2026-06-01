"""Local secret template for the standalone legal pipeline.

Copy this file to ``legal/secret.py`` and fill in real values. ``secret.py``
is gitignored and must never be committed. Values may also be supplied via the
environment (``LEGAL_<NAME>``, e.g. ``LEGAL_CAPSOLVER_API_KEY``), which takes
precedence over this module.
"""

# Capsolver (https://capsolver.com) - captcha solving API key.
CAPSOLVER_API_KEY = ""

# Floxy (mobile proxy) credentials.
FLOXY_USER = ""
FLOXY_PASS = ""
