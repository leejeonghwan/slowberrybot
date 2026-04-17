"""Gunicorn 진입점."""

from daum_trends_web.app import create_app_from_env

app = create_app_from_env()
