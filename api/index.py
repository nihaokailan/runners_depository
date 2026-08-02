"""Vercel entry point for the existing standard-library web application."""

from app import RequestHandler, init_db


# Vercel invokes a BaseHTTPRequestHandler subclass for each request. Initialising
# here creates the Postgres schema on a fresh function instance without starting
# the local development server.
init_db()


class handler(RequestHandler):
    pass
