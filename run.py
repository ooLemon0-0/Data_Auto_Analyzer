from app.api import app

if __name__ == "__main__":
    import uvicorn
    from app.core.config import settings

    uvicorn.run(
        "app.api:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
    )
