from typing import List, Dict, TypedDict, Any
from pydantic import BaseModel, field_validator
import json

class CreateCamera(BaseModel):
    title: str
    source: str
    image_width: int
    image_height: int
    calib: Any
    latitude: float
    longitude: float
    
    @field_validator('title')
    @classmethod
    def validate_title(cls, title):
        if len(title) < 1 or len(title) > 200:
            raise ValueError(f"Invalid camera title: {title}")
        return title

    @field_validator('source')
    @classmethod
    def validate_source(cls, source):
        return source
    
    @field_validator('latitude')
    @classmethod
    def validate_latitude(cls, latitude):
        if latitude > 90 or latitude < -90:
            raise ValueError(f"Invalid latitude value: {latitude}")
        return latitude
    
    @field_validator('longitude')
    @classmethod
    def validate_longitude(cls, longitude):
        if longitude > 180 or longitude < -180:
            raise ValueError(f"Invalid longitude value: {longitude}")
        return longitude
    
    @field_validator('image_width')
    @classmethod
    def validate_image_width(cls, image_width):
        if image_width <= 0: 
            raise ValueError(f"Invalid image_width value: {image_width}")
        return image_width
    
    @field_validator('image_height')
    @classmethod
    def validate_image_height(cls, image_height):
        if image_height <= 0: 
            raise ValueError(f"Invalid image_height value: {image_height}")
        return image_height
    
    @field_validator('calib')
    @classmethod
    def validate_calib(cls, calib):
        if calib is not None:
            try:
                json.dumps(calib)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Invalid calibration data: {e}")
        return calib
    
class Point(BaseModel):
    latitude: float
    longitude: float
    x: int
    y: int

    def __eq__(self, other):
        if not isinstance(other, Point):
            return False
        return (self.x == other.x and self.y == other.y)

    @field_validator('latitude')
    @classmethod
    def validate_latitude(cls, latitude):
        if latitude > 90 or latitude < -90:
            raise ValueError(f"Invalid latitude value: {latitude}")
        return latitude
    
    @field_validator('longitude')
    @classmethod
    def validate_longitude(cls, longitude):
        if longitude > 180 or longitude < -180:
            raise ValueError(f"Invalid longitude value: {longitude}")
        return longitude
    
    @field_validator('x')
    @classmethod
    def validate_x(cls, x):
        if x < 0: 
            raise ValueError(f"Invalid x value: {x}")
        
        return x

    @field_validator('y')
    @classmethod
    def validate_y(cls, y):
        if y < 0: 
            raise ValueError(f"Invalid y value: {y}")
        
        return y

class CreateZone(BaseModel):
    camera_id: int
    zone_type: str
    capacity: int
    pay: int
    points: List[Point]

    @field_validator('camera_id')
    @classmethod
    def validate_camera_id(cls, camera_id):
        if camera_id <= 0:
            raise ValueError(f"Invalid camera_id value: {camera_id}")
        
        return camera_id
    
    @field_validator('zone_type')
    @classmethod
    def validate_zone_type(cls, zone_type):
        if zone_type not in ['parallel', 'standard']:
            raise ValueError(f"Invalid zone_type value: {zone_type}")
        
        return zone_type
    
    @field_validator('capacity')
    @classmethod
    def validate_capacity(cls, capacity):
        if capacity <= 0:
            raise ValueError(f"Invalid capacity value: {capacity}")
        
        return capacity
        
    @field_validator('pay')
    @classmethod
    def validate_pay(cls, pay):
        if pay < 0:
            raise ValueError(f"Invalid pay value: {pay}")
        
        return pay
    
    @field_validator('points')
    @classmethod
    def validate_points(cls, points):
        if len(points) != 4:
            raise ValueError(f"Invalid points count: {len(points)}")
        
        for lhs in range(0, len(points)):
            for rhs in range(lhs + 1, len(points)):
                if points[lhs] == points[rhs]:
                    raise ValueError(f"Degenerate rectangle")
        
        return points