from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

Base = declarative_base()

class Camera(Base):
    __tablename__ = 'cameras'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120))
    is_active = Column(Boolean, default=False)
    pixels_height = Column(Integer)
    pixels_width = Column(Integer)
    created_at = Column(DateTime, default=timezone.utc)
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=timezone.utc)
    
    parking_zones = relationship("ParkingZone", back_populates="camera")
    cars = relationship("Car", back_populates="camera")
    
    def __repr__(self):
        return f"<Camera(id={self.id}, title='{self.title}', is_active={self.is_active})>"

class ParkingZone(Base):
    __tablename__ = 'parking_zones'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_type = Column(String(50))
    parking_lots_count = Column(Integer)
    camera_id = Column(Integer, ForeignKey('cameras.id'))
    
    camera = relationship("Camera", back_populates="parking_zones")
    points = relationship("ParkingZonePoint", back_populates="parking_zone")
    
    def __repr__(self):
        return f"<ParkingZone(id={self.id}, zone_type='{self.zone_type}', camera_id={self.camera_id})>"

class ParkingZonePoint(Base):
    __tablename__ = 'parking_zones_points'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    parking_zone_id = Column(Integer, ForeignKey('parking_zones.id'))
    x_component = Column(Numeric(6, 5))
    y_component = Column(Numeric(6, 5))
    latitude = Column(Numeric(11, 8))
    longitude = Column(Numeric(11, 8))
    
    parking_zone = relationship("ParkingZone", back_populates="points")
    
    def __repr__(self):
        return f"<ParkingZonePoint(id={self.id}, zone_id={self.parking_zone_id}, x={self.x_component}, y={self.y_component})>"

class Car(Base):
    __tablename__ = 'cars'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    confidence_rate = Column(Numeric(5, 4))
    camera_id = Column(Integer, ForeignKey('cameras.id'))
    
    camera = relationship("Camera", back_populates="cars")
    points = relationship("CarPoint", back_populates="car")
    
    def __repr__(self):
        return f"<Car(id={self.id}, confidence={self.confidence_rate}, camera_id={self.camera_id})>"

class CarPoint(Base):
    __tablename__ = 'car_points'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    car_id = Column(Integer, ForeignKey('cars.id'))
    x_component = Column(Numeric(6, 5))
    y_component = Column(Numeric(6, 5))
    
    car = relationship("Car", back_populates="points")
    
    def __repr__(self):
        return f"<CarPoint(id={self.id}, car_id={self.car_id}, x={self.x_component}, y={self.y_component})>"