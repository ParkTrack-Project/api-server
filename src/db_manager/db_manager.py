from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
import contextlib
from typing import Generator, Optional
import os

from .models import Base

class DBManager:
    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or self._get_default_database_url()
        self.engine = None
        self.SessionLocal = None
        self._initialize_database()

    def _get_default_database_url(self) -> str:
        """Захардкодил URL базы данных по умолчанию"""
        # Для SQLite
        return "sqlite:///./parktrack.db"
        
        # Для PostgreSQL:
        # return "postgresql://user:password@localhost/parktrack"

    def _initialize_database(self):
        """Инициализация движка и сессии"""
        try:
            # Для SQLite нужно special pragma для Foreign Keys
            connect_args = {}
            if self.database_url.startswith("sqlite"):
                connect_args = {"check_same_thread": False}
            
            self.engine = create_engine(
                self.database_url,
                connect_args=connect_args,
                echo=True,  # Для тестирования
                pool_pre_ping=True # Загадочно
            )
            
            self.SessionLocal = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self.engine
            )
            
            if not self._check_tables_exist():
                print(f"Gotta setup database real quick hold on...")
                self._create_tables()
            
            print(f"Database initialized successfully: {self.database_url}")
            
        except Exception as e:
            print(f"Database initialization failed: {e}")
            raise

    def _check_tables_exist(self) -> bool:
        try:
            inspector = inspect(self.engine)
            existing_tables = inspector.get_table_names()
            
            # Получаем список таблиц, которые должны быть созданы
            required_tables = Base.metadata.tables.keys()
            
            print(f"Existing tables: {existing_tables}")
            print(f"Required tables: {list(required_tables)}")
            
            # Проверяем, что ВСЕ нужные таблицы существуют
            for table_name in required_tables:
                if table_name not in existing_tables:
                    print(f"Table {table_name} not found")
                    return False
                
            return True
            
        except Exception as e:
            print(f"Error checking tables: {e}")
            return False

    def _create_tables(self):
        try:
            print("Creating database tables...")
            Base.metadata.create_all(bind=self.engine)
            print("Tables created successfully")
        except Exception as e:
            print(f"Error creating tables: {e}")
            raise

    @contextlib.contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """Контекстный менеджер для получения сессии"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def check_connection(self) -> bool:
        """Проверить соединение с базой данных"""
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as e:
            print(f"Database connection check failed: {e}")
            return False

    def get_db(self) -> Generator[Session, None, None]:
        return self.get_session()

    def execute_raw_sql(self, query: str, params: dict = None):
        """Выполнить сырой SQL запрос"""
        with self.get_session() as session:
            result = session.execute(query, params or {})
            return result