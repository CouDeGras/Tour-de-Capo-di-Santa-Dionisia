"""ASGI config for the Capo di Santa Dionisia dashboard project.

Kept alongside wsgi.py (the standard django-admin startproject layout) even
though the dashboard is currently served over WSGI -- an ASGI entrypoint is
what future realtime control features (e.g. Django Channels for pushing
pump-ack updates instead of the dashboard's 15s poll) would build on.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

application = get_asgi_application()
