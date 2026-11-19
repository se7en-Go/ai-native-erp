from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str


class AppError(Exception):
    def __init__(
        self,
        *,
        slug: str,
        title: str,
        status_code: int,
        detail: str,
        code: str,
    ) -> None:
        super().__init__(detail)
        self.slug = slug
        self.title = title
        self.status_code = status_code
        self.detail = detail
        self.code = code


def register_exception_handlers(app: FastAPI) -> None:
    async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, AppError):
            raise exc
        problem = ProblemDetail(
            type=f"https://erp.local/errors/{exc.slug}",
            title=exc.title,
            status=exc.status_code,
            detail=exc.detail,
            instance=str(request.url.path),
            code=exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        problem = ProblemDetail(
            type="https://erp.local/errors/validation-error",
            title="Request Validation Failed",
            status=422,
            detail=str(exc.errors()),
            instance=str(request.url.path),
            code="ERR_VALIDATION",
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
