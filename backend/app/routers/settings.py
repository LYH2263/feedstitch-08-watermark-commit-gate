from fastapi import APIRouter

from app.db import connect
from app.repositories import settings as settings_repo
from app.schemas.entities import SettingUpdate

router = APIRouter(tags=["settings"])


@router.get("/settings")
def list_settings():
    conn = connect()
    try:
        return settings_repo.as_list(conn)
    finally:
        conn.close()


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingUpdate):
    conn = connect()
    try:
        settings_repo.set_value(conn, key, body.value)
        conn.commit()
        return {"key": key, "value": body.value}
    finally:
        conn.close()


@router.get("/watermarks")
def list_watermarks():
    from app.modules import watermark_commit_gate

    conn = connect()
    try:
        return watermark_commit_gate.watermark_status(conn)
    finally:
        conn.close()
