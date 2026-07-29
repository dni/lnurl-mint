import logging
from http import HTTPStatus
from typing import Any, Callable, Sequence

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


def _error(reason: str) -> JSONResponse:
    # LNURL errors always return status 200 with {"status": "ERROR", ...}
    return JSONResponse(status_code=HTTPStatus.OK, content={"status": "ERROR", "reason": reason})


class LnurlErrorResponseHandler(APIRoute):
    """Route class that converts any HTTPException (and validation errors)
    into the LUD-standard {"status": "ERROR", "reason": ...} JSON body."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # None means "unset" in LNURL responses (a melt's success response
        # carries no k1/change) - wallets should see those fields absent,
        # not present-with-null. FastAPI always passes this explicitly
        # (defaulting to False), so it must be overwritten, not setdefault'd.
        kwargs["response_model_exclude_none"] = True
        super().__init__(*args, **kwargs)

    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        def validation_errors_to_string(errors: Sequence) -> str:
            return ", ".join([f"{error['msg']} `{error['loc'][1]}`" for error in errors])

        async def lnurl_route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except HTTPException as exc:
                return _error(f"{exc.detail}")
            except (RequestValidationError, ResponseValidationError) as exc:
                prefix = "Request" if isinstance(exc, RequestValidationError) else "Response"
                return _error(f"{prefix} validation error: {validation_errors_to_string(exc.errors())}.")
            except Exception as exc:
                logging.error(exc, exc_info=True)
                return _error(f"{exc!s}")

        return lnurl_route_handler
