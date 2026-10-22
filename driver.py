from shared.app import create_app
import os

def main():
    create_app().run(debug=os.getenv("DEBUG", "0") == "1")

if __name__ == "__main__":
    main()