"""Analyst console: a local backend-for-frontend (BFF) and the dashboard deployment tooling.

The browser never receives the scoring API key: it talks only to the console service, which
authenticates analysts with a session cookie and calls the scoring API server-side.
"""
