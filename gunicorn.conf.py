"""Single-VM deployment; Nginx is the only public listener."""
import os

if len(os.environ.get('SECRET_KEY', '')) < 32:
    raise RuntimeError('Set SECRET_KEY to a stable random secret of at least 32 characters.')

bind = '127.0.0.1:5000'
workers = int(os.environ.get('WEB_CONCURRENCY', '2'))
worker_class = 'gthread'
threads = int(os.environ.get('GUNICORN_THREADS', '4'))
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = '-'
errorlog = '-'
capture_output = True
# Trust HTTPS headers only from the local reverse proxy.
forwarded_allow_ips = '127.0.0.1'
