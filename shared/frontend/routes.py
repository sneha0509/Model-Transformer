from flask import Blueprint, redirect, render_template, request, session, url_for


shared_frontend = Blueprint(
    "portal", __name__, template_folder="templates", static_folder="static",
    static_url_path="/shared/static",
)


def require_page_login():
    if request.endpoint and request.endpoint.endswith(".static"):
        return None
    if not session.get("user"):
        return redirect(url_for("portal.home"))


@shared_frontend.app_context_processor
def portal_context():
    return {"portal_user": session.get("user")}


@shared_frontend.get("/")
def home():
    return render_template("portal/home.html")