import os
from sqlalchemy import create_engine, inspect, text, func, update
from sqlalchemy.orm import sessionmaker, Session, joinedload
from sqlalchemy.exc import SQLAlchemyError
import contextlib
from typing import Generator, Optional

from .models import Base, Camera, ParkingZone, ParkingZonePoint, datetime, timezone

class DBManager:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.SessionLocal = None
        self._initialize_database()

        self.total_camera_count = None
        self.camera_index = 1

    # def _get_default_database_url(self) -> str:
    #     """Захардкодил URL базы данных по умолчанию"""
    #     # Для SQLite
    #     # return "sqlite:///./parktrack.db"
        
    #     # Для PostgreSQL:
    #     # return "postgresql://user:password@localhost/parktrack"
    #     return os.getenv("DB_CONNECTION_URL")

    def _get_total_camera_count(self):
        if not self.total_camera_count: 
            with self.get_session() as session:
                self.total_camera_count = session.query(func.count(Camera.id)).scalar()

        return self.total_camera_count
    
    def _next_camera(self):
        if self.camera_index == self._get_total_camera_count():
            self.camera_index = 1
        else:
            self.camera_index += 1

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
        
    def camera_title_already_exists(self, title) -> bool:
        with self.get_session() as session:
            query_result = session.query(Camera).filter(Camera.title.ilike(title))

            return session.query(query_result.exists()).scalar()
        
    def camera_id_exists(self, id) -> bool:
        with self.get_session() as session:
            query_result = session.query(Camera).filter(Camera.id == id)

            return session.query(query_result.exists()).scalar()
        
    def create_camera(self, camera):
        with self.get_session() as session:
            new_camera = Camera(
                title=camera['title'],
                latitude=camera['latitude'],
                longitude=camera['longitude'],
                source=camera['source'],
                image_height=camera['image_height'],
                image_width=camera['image_width'],
                calib=camera['calib']
            )

            session.add(new_camera)

            session.flush()

            return new_camera.id
        
    def create_zone(self, zone):
        with self.get_session() as session:
            new_zone = ParkingZone(
                zone_type=zone['zone_type'],
                parking_lots_count=zone['parking_lots_count'],
                camera_id=zone['camera_id'],
                pay=zone['pay']
            )

            session.add(new_zone)

            session.flush()

            session.add_all([
                ParkingZonePoint(
                    parking_zone_id=new_zone.id, 
                    x=point.x,
                    y=point.y,
                    latitude=point.latitude,
                    longitude=point.longitude)
                for point in zone['points']
            ])

            session.flush()

            return new_zone.id
        
    def get_zone(self, zone_id: int, with_points=False):
        with self.get_session() as session:
            query = session.query(ParkingZone)

            if with_points:
                query = query.options(joinedload(ParkingZone.points))

            zone = query.filter(ParkingZone.id == zone_id).one_or_none()

            return zone.serialize() if zone is not None else zone
    
    def get_all_zones(self, camera_id, min_free_count, max_pay):
        with self.get_session() as session:
            query = session.query(ParkingZone).options(joinedload(ParkingZone.points))

            if camera_id is not None:
                query = query.filter(ParkingZone.camera_id == camera_id)
            
            if min_free_count is not None and min_free_count > 0:
                query = query.filter(ParkingZone.parking_lots_count - ParkingZone.occupied >= min_free_count)

            if max_pay is not None:
                query = query.filter(ParkingZone.pay <= max_pay)

            return [zone.serialize() for zone in query.all()]
        
    def get_all_cameras(
            self, 
            q, 
            top_left_corner_latitude, 
            top_left_corner_longitude, 
            bottom_right_corner_latitude, 
            bottom_right_corner_longitude):
        with self.get_session() as session:
            query = session.query(Camera)

            if q is not None:
                query = query.filter(Camera.title.icontains(q))
            
            if top_left_corner_latitude is not None:
                query = query.filter(Camera.latitude <= top_left_corner_latitude)

            if top_left_corner_longitude is not None:
                query = query.filter(Camera.longitude >= top_left_corner_longitude)

            if bottom_right_corner_latitude is not None:
                query = query.filter(Camera.latitude >= bottom_right_corner_latitude)

            if bottom_right_corner_longitude is not None:
                query = query.filter(Camera.longitude >= bottom_right_corner_longitude)

            return [camera.serialize() for camera in query.all()]

    def get_most_outdated_camera(self, limit: int = 1):
        with self.get_session() as session:
            camera = session.query(Camera).filter(Camera.id == self.camera_index).one_or_none()

            self._next_camera()

            return camera.serialize() if camera is not None else {}
        
    def get_camera(self, camera_id):
        with self.get_session() as session:
            camera = session.query(Camera).filter(Camera.id == camera_id).one_or_none()

            return camera.serialize()

    def update_camera(self, camera_id, updated_fields):
        with self.get_session() as session:
            stmt = update(Camera).where(Camera.id == camera_id)

            stmt = stmt.values(updated_fields)
            print(updated_fields)

            session.execute(stmt)

            session.commit()

            camera = session.query(Camera).filter(Camera.id == camera_id).one_or_none()

            return camera.serialize()
        
    def update_zone(self, zone_id, updated_fields):
        with self.get_session() as session:
            stmt = update(ParkingZone).where(ParkingZone.id == zone_id)

            stmt = stmt.values(
                updated_fields | {"updated_at": datetime.now(timezone.utc)}
                    if "occupied" not in updated_fields else 
                updated_fields | {"occupancy_updated_at": datetime.now(timezone.utc)})
            
            session.execute(stmt)

            session.commit()

            zone = session.query(ParkingZone).filter(ParkingZone.id == zone_id).one_or_none()

            return zone.serialize()