from api.api import PublicAPI, URL
from db_manager.db_manager import DBManager

def main():
    api_server = PublicAPI(db_manager=DBManager("mock"))
    api_server.run(URL(host="0.0.0.0", port=8000))

if __name__ == "__main__":
    main()