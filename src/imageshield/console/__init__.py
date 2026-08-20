"""The control-room console — a separate internal ops deployable.

Server-rendered Jinja2 over the services admin API and the fetcher. It holds
NO database access of any kind: every read and every write flows through
HTTP, with tokens held server-side and never echoed to the browser. See
``imageshield.console.app.create_app``.
"""
