from api.api import PublicAPI, URL
from db_manager.db_manager import DBManager

def main():
    api_server = PublicAPI(DBManager())
    api_server.run(URL(host="127.0.0.1", port="8000"))

if __name__ == "__main__":
    main()