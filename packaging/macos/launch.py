"""Finder entry point for the bundled application."""
import os
import sys

import certifi

from framesync.app import main

# The embedded interpreter cannot rely on the build machine's OpenSSL CA path.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

if __name__ == "__main__":
    sys.exit(main())
