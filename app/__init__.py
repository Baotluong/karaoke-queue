import os

import certifi

# Homebrew/python.org Python on macOS may not find the system CA certificates,
# so HTTPS to YouTube fails with CERTIFICATE_VERIFY_FAILED. aiohttp builds its
# SSL context at import time, so this must run before anything imports aiohttp.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
