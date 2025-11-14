from api.api import PublicAPI, URL
from db_manager.db_manager import DBManager
import os

def main():
    api_server = PublicAPI(DBManager())
    api_server.run(URL(host=os.getenv("HOST"), port=os.getenv("PORT")))

if __name__ == "__main__":
    main()