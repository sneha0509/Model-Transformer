from ui import app
from dotenv import load_dotenv
import os

def main():
    load_dotenv(dotenv_path=".env", override=False)
    app.model_transformer.run(debug=int(os.getenv("DEBUG")))

if __name__ == "__main__":
    main()