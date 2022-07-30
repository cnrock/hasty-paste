import secrets
from collections.abc import AsyncGenerator
from datetime import datetime
from functools import wraps
from pathlib import Path

from aiofiles import open as aio_open
from aiofiles import os as aio_os
from aiofiles import ospath as aio_ospath
from pydantic import BaseModel, ValidationError
from quart import Response, abort, make_response

CURRENT_PASTE_META_VERSION = 1


class PasteException(Exception):
    pass


class PasteMetaException(Exception):
    pass


class PasteIdException(PasteException):
    pass


class PasteDoesNotExistException(PasteException):
    pass


class PasteExpiredException(PasteException):
    pass


class PasteMetaUnprocessable(PasteMetaException):
    pass


class PasteMetaVersionInvalid(PasteMetaException):
    pass


class PasteMetaVersion(BaseModel):
    version: int = CURRENT_PASTE_META_VERSION


class PasteMeta(PasteMetaVersion):
    paste_id: str
    creation_dt: datetime
    expire_dt: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.expire_dt is not None and self.expire_dt < datetime.utcnow():
            return True
        return False


class PasteMetaCreate(BaseModel):
    content: bytes
    long_id: bool = False
    expire_dt: datetime | None = None


def get_paste_meta(meta_line: bytes) -> PasteMeta:
    try:
        version = PasteMetaVersion.parse_raw(meta_line).version
        # NOTE this allows for future support if the meta format was to change
        if version != CURRENT_PASTE_META_VERSION:
            raise PasteMetaVersionInvalid(f"paste is not a valid version number of '{version}'")
        return PasteMeta.parse_raw(meta_line)
    except ValidationError as err:
        raise PasteMetaUnprocessable("paste meta cannot be loaded") from err


def create_paste_id(long: bool = False) -> str:
    if long:
        return secrets.token_urlsafe(30)
    return secrets.token_urlsafe(7)


def create_paste_path(root_path: Path, paste_id: str, mkdir: bool = False) -> Path:
    if len(paste_id) < 3:
        raise PasteIdException("paste_id too short, must be at least 3 characters long")
    full_path = root_path / paste_id[:2]
    if mkdir:
        full_path.mkdir(parents=True, exist_ok=True)
    return full_path / paste_id[2:]


async def write_paste(
        paste_path: Path,
        paste_meta: PasteMeta,
        content: AsyncGenerator[bytes, None] | bytes):
    async with aio_open(paste_path, "wb") as fo:
        await fo.write(paste_meta.json().encode() + b"\n")
        if isinstance(content, AsyncGenerator):
            async for chunk in content:
                await fo.write(chunk)
        else:
            await fo.write(content)


async def read_paste_meta(paste_path: Path) -> PasteMeta:
    async with aio_open(paste_path, "rb") as fo:
        meta = get_paste_meta(await fo.readline())
        return meta


async def read_paste_content(paste_path: Path) -> AsyncGenerator[bytes, None]:
    async with aio_open(paste_path, "rb") as fo:
        # TODO use tell+seek+read to save memory while checking for newline
        _ = await fo.readline()
        async for line in fo:
            yield line


def get_form_datetime(value: str) -> datetime | None:
    if value:
        return datetime.fromisoformat(value)


async def try_get_paste(
        root_path: Path,
        paste_id: str,
        auto_remove: bool = True,
    ) -> tuple[Path, PasteMeta]:
    paste_path = create_paste_path(root_path, paste_id)

    if not await aio_ospath.isfile(paste_path):
        raise PasteDoesNotExistException(f"paste not found with id of {paste_id}")

    paste_meta = await read_paste_meta(paste_path)

    if paste_meta.is_expired:
        if auto_remove:
            try:
                await aio_os.remove(paste_path)
            except FileNotFoundError:
                pass
        raise PasteExpiredException(f"paste has expired with id of {paste_id}")

    return paste_path, paste_meta


async def try_get_paste_with_content_response(
        root_path: Path,
        paste_id: str,
        auto_remove: bool = True,
    ) -> tuple[Path, PasteMeta, Response]:
    paste_path, paste_meta = await try_get_paste(root_path, paste_id, auto_remove)

    response = await make_response(read_paste_content(paste_path))
    response.mimetype="text/plain"

    return paste_path, paste_meta, response


def handle_paste_exceptions(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except PasteException:
            abort(404)
        except PasteMetaException:
            abort(500)
    return wrapper
