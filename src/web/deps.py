from fastapi import Request

from db.database import get_connection


def get_db(request: Request):
    conn = get_connection(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_supervisor(request: Request):
    return request.app.state.supervisor


def get_templates(request: Request):
    return request.app.state.templates
