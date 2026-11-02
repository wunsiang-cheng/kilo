"""Server package."""

# Export app lazily or allow package-level access
def get_app():
    from kilo.server.app import app
    return app
