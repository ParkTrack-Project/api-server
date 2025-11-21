from api.api import PublicAPI, URL
from db_manager.db_manager import DBManager
import os

from dotenv import load_dotenv

load_dotenv()

def main():
    print(os.getenv("DB_CONNECTION_URL"), os.getenv("HOST"), os.getenv("PORT"))
    api_server = PublicAPI(DBManager(os.getenv("DB_CONNECTION_URL")))
    api_server.run(URL(host=os.getenv("HOST"), port=os.getenv("PORT")))

if __name__ == "__main__":
    main()