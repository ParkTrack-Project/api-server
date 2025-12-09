from typing import List, Dict, TypedDict, Any, Optional, Literal
from pydantic import BaseModel, field_validator, Field
import json
    
class CameraBase(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=120)
    source: Optional[str] = Field(None, max_length=250)
    image_width: Optional[int] = Field(None, gt=0)
    image_height: Optional[int] = Field(None, gt=0)
    calib: Optional[Any] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    is_active: Optional[bool] = None

    @field_validator('calib')
    @classmethod
    def validate_calib(cls, calib):
        if calib is not None:
            try:
                json.dumps(calib)
            except:
                raise ValueError(f"Invalid calibration data")
        return calib
    
class CreateCamera(CameraBase):
    title: str = Field(min_length=3, max_length=120)
    source: str = Field(max_length=250)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    calib: Any
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

class Point(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    x: int = Field(ge=0)
    y: int = Field(ge=0)

    def __eq__(self, other):
        if not isinstance(other, Point):
            return False
        return (self.x == other.x and self.y == other.y)

class ZoneBase(BaseModel):
    camera_id: Optional[int] = Field(None, ge=1)
    zone_type: Optional[Literal['parallel', 'standard']] = None
    capacity: Optional[int] = Field(None, gt=0)
    pay: Optional[int] = Field(None, ge=0)
    points: Optional[List[Point]] = None

    @field_validator('points')
    @classmethod
    def validate_points(cls, v: Optional[List[Point]]) -> Optional[List[Point]]:
        if v is None:
            return v
            
        if len(v) != 4:
            raise ValueError(f"Invalid points count: {len(v)}. Must be exactly 4 points")
        
        for lhs in range(0, 4):
            for rhs in range(lhs + 1, 4):
                if v[lhs] == v[rhs]:
                    raise ValueError(f"Degenerate rectangle")
            
        return v

class CreateZone(ZoneBase):
    camera_id: int
    zone_type: Literal['parallel', 'standard']
    capacity: int = Field(gt=0)
    pay: int = Field(ge=0)
    points: List[Point]

class UpdateZone(ZoneBase):
    occupied: Optional[int] = Field(None, ge=0)
    confidence: Optional[float] = Field(None, ge=0, le=1)