import json
from pathlib import Path

from flask import Flask, render_template


app = Flask(__name__, static_folder="templates/static")
SAMPLE_DATA_PATH = Path(__file__).resolve().parent.parent / "sample.json"


def get_mock_portal_data():
    with SAMPLE_DATA_PATH.open(encoding="utf-8") as sample_file:
        return json.load(sample_file)


@app.route("/")
def index():
    return render_template("index.html", app_data=get_mock_portal_data())


def main():
    app.run(debug=True)


if __name__ == "__main__":
    main()
