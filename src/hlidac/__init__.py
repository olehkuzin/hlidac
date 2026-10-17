"""Digital side-effect watchdog for Rakathon 2026 (praha-03)."""


def main() -> None:
    import uvicorn

    uvicorn.run("hlidac.app:app", host="127.0.0.1", port=8000, reload=True)
