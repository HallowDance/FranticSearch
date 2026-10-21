"""gunicorn settings for the live instance (deploy/systemd/franticsearch.service).

    gunicorn -c deploy/gunicorn.conf.py franticsearch.wsgi:app

Settings live here rather than on the unit's ExecStart because systemd would
read the ``%`` in the log format as its own specifiers.
"""

bind = "127.0.0.1:6602"   # the reverse SSH tunnel forwards to this port
# One process on purpose: the Scryfall auto-sync thread must run once, and SQLite
# has one writer. Threads cover concurrency (docs/SCALING.md § Stage 2).
workers = 1
threads = 8
timeout = 120             # a large collection CSV import can take a while
graceful_timeout = 30
accesslog = "-"
# The journal timestamps every line itself; %(M)s = response time in milliseconds.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
control_socket_disable = True   # not used; its default path is shared by every gunicorn
