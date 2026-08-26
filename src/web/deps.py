from fastapi import Request

from db.database import get_connection


def get_db(request: Request):
    return get_connection(request.app.state.db_path)


def get_supervisor(request: Request):
    return request.app.state.supervisor


def get_templates(request: Request):
    return request.app.state.templates
