"""Vercel entry point for the existing standard-library web application."""

import urllib.parse

from app import RequestHandler, init_db


# Vercel invokes a BaseHTTPRequestHandler subclass for each request. Initialising
# here creates the Postgres schema on a fresh function instance without starting
# the local development server.
init_db()


class handler(RequestHandler):
    """Restore the public URL after Vercel routes it to this function."""

    def _restore_public_path(self):
        parsed = urllib.parse.urlparse(self.path)
        query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        routed_paths = [value for key, value in query_items if key == "__path"]
        remaining_query = [(key, value) for key, value in query_items if key != "__path"]

        if routed_paths and routed_paths[-1]:
            public_path = "/" + urllib.parse.unquote(routed_paths[-1]).lstrip("/")
        elif parsed.path.startswith("/api"):
            # Any bare invocation of the function (/api, /api/index, /api/index.py,
            # etc.) with no meaningful __path segment maps to the site root. Vercel
            # strips the .py extension internally, so the exact string varies -
            # match on prefix instead of an exact tuple of strings.
            public_path = "/"
        else:
            return

        self.path = public_path
        if remaining_query:
            self.path += "?" + urllib.parse.urlencode(remaining_query)

    def do_GET(self):
        self._restore_public_path()
        super().do_GET()

    def do_POST(self):
        self._restore_public_path()
        super().do_POST()