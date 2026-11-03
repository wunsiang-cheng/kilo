"""
Files sent from the web terminal, saved in uploads/ in the workspace (kilo/uploads.py) to be attached to a message.
The request body is the file itself rather than a multipart form, so it is written as it arrives and refused the
moment it passes the limit; a form is read whole into a temporary file before any of it can be checked.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from kilo import uploads
from kilo.config import settings
from kilo.models.session import Attachment

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"File is larger than the upload limit of {settings.kilo_upload_max_mb} MB (KILO_UPLOAD_MAX_MB).",
    )


@router.post("", response_model=Attachment, status_code=201)
async def upload(request: Request, name: Optional[str] = Query(None)) -> Attachment:
    """
    Save the request body, one file's bytes, in uploads/ under `name` made safe. Returns where it went, to send
    in a chat request's `attachments`.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name is required.")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > uploads.max_bytes():
        raise _too_large()  # refused before any of it is read
    try:
        return await uploads.save(request.stream(), name, request.headers.get("content-type"))
    except uploads.UploadTooLarge:
        raise _too_large()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not save {name}: {e}")
