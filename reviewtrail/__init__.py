"""ReviewTrail package metadata and workbench HTTP hardening."""

from .hosting import install_http_hardening

__version__ = '1.0.0'

# app.py uses BaseHTTPRequestHandler directly. Install the narrowly scoped
# workbench safeguards before its Handler subclass is created. The wrappers
# identify the workbench by server_version and leave demo.py unchanged.
install_http_hardening()
