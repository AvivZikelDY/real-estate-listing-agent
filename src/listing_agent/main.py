"""FastAPI application bootstrap for the Tel Aviv Real Estate Listing Triage Agent."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.listing_agent.api.routes import router as api_router
from src.listing_agent.api.schemas import ExecuteResponse

GUI_DIR = Path(__file__).resolve().parent.parent.parent / "gui"

app = FastAPI(title="Tel Aviv Real Estate Listing Triage Agent")

app.include_router(api_router)
app.mount("/static", StaticFiles(directory=GUI_DIR / "static"), name="static")


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Invalid requests (e.g. a blank `prompt`) still get the four-field shape."""
    return JSONResponse(
        status_code=422,
        content=ExecuteResponse(
            status="error", error=str(exc), response=None, steps=[]
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort safety net: any unhandled exception still returns the
    four required top-level fields instead of an opaque 500 page."""
    return JSONResponse(
        status_code=500,
        content=ExecuteResponse(
            status="error",
            error=f"Unexpected error: {exc}",
            response=None,
            steps=[],
        ).model_dump(),
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "healthy"}


@app.get("/")
def root() -> FileResponse:
    """Serve the minimal placeholder GUI page."""
    return FileResponse(GUI_DIR / "index.html")

