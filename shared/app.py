import os
import secrets

from dotenv import load_dotenv
from flask import Flask

from shared.paths import PROJECT_ROOT


_SESSION_SECRET = secrets.token_hex(32)


def create_app(config=None):
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    app = Flask(__name__, static_folder=None, template_folder=None)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or _SESSION_SECRET,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config is not None:
        app.config.update(config)

    from shared.backend.routes import shared_backend
    from shared.frontend.routes import shared_frontend
    from legacy.backend.routes import legacy_backend
    from legacy.frontend.routes import legacy_frontend
    from fabric.backend.routes import fabric_backend
    from fabric.frontend.routes import fabric_frontend

    for blueprint in (
        shared_backend, shared_frontend,
        legacy_backend, legacy_frontend,
        fabric_backend, fabric_frontend,
    ):
        app.register_blueprint(blueprint)

    return app
