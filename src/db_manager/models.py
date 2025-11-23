from sqlalchemy import Column, Integer, String, Boolean, DateTime, Numeric, ForeignKey, Float, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

Base = declarative_base()

class Camera(Base):
    __tablename__ = 'cameras'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120))
    is_active = Column(Boolean, default=True)
    source = Column(String(250))
    image_height = Column(Integer, default=0)
    image_width = Column(Integer, default=0)
    calib = Column(JSON, default=None)
    latitude = Column(Float(precision=6))
    longitude = Column(Float(precision=6))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    parking_zones = relationship("ParkingZone", back_populates="camera")
    cars = relationship("Car", back_populates="camera")
    
    def __repr__(self):
        return f"<Camera(id={self.id}, title='{self.title}', is_active={self.is_active})>"
    
    def serialize(self):
        return {
        "camera_id": self.id,
        "title": self.title,
        "is_active": self.is_active,
        "source": self.source,
        "image_height": self.image_height,
        "image_width": self.image_width,
        "calib": self.calib,
        "latitude": self.latitude,
        "longitude": self.longitude,
        "created_at": self.created_at.isoformat() if self.created_at else None,
        "updated_at": self.updated_at.isoformat() if self.updated_at else None
    }

    def serialize_metadata_only(self):
        return {
        "camera_id": self.id,
        "source": self.source,
        "image_height": self.image_height,
        "image_width": self.image_width,
        "calib": self.calib
    }

class ParkingZone(Base):
    __tablename__ = 'parking_zones'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_type = Column(String(50))
    parking_lots_count = Column(Integer)
    camera_id = Column(Integer, ForeignKey('cameras.id'))
    occupied = Column(Integer, default=None)
    confidence = Column(Float(precision=6), default=None)
    pay = Column(Integer)
    occupancy_updated_at = Column(DateTime, default=None)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    camera = relationship("Camera", back_populates="parking_zones")
    points = relationship("ParkingZonePoint", back_populates="parking_zone")
    
    def __repr__(self):
        return f"<ParkingZone(id={self.id}, zone_type='{self.zone_type}', camera_id={self.camera_id})>"
    
    def serialize(self):
        data = {
            "zone_id": self.id,
            "camera_id": self.camera_id,
            "zone_type": self.zone_type,
            "capacity": self.parking_lots_count,
            "occupied": self.occupied,
            "confidence": self.confidence,
            "pay": self.pay,
            "occupancy_updated_at": self.occupancy_updated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }
            
        data["points"] = [point.serialize() for point in self.points]
                
        return data

class ParkingZonePoint(Base):
    __tablename__ = 'parking_zones_points'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    parking_zone_id = Column(Integer, ForeignKey('parking_zones.id'))
    x = Column(Integer)
    y = Column(Integer)
    latitude = Column(Numeric(11, 8))
    longitude = Column(Numeric(11, 8))
    
    parking_zone = relationship("ParkingZone", back_populates="points")
    
    def __repr__(self):
        return f"<ParkingZonePoint(id={self.id}, zone_id={self.parking_zone_id}, x={self.x_component}, y={self.y_component})>"
    
    def serialize(self):
        return {
            "id": self.id,
            "parking_zone_id": self.parking_zone_id,
            "x": self.x,
            "y": self.y,
            "latitude": float(self.latitude) if self.latitude else None,
            "longitude": float(self.longitude) if self.longitude else None
        }

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