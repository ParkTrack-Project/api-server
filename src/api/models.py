from pydantic import BaseModel, field_validator

class CreateCamera(BaseModel):
    title: str
    latitude: float
    longitude: float
    
    @field_validator('title')
    @classmethod
    def validate_title(cls, title):
        if len(title) < 1 or len(title) > 200:
            raise ValueError(f"Invalid camera title")
        return title

    @field_validator('latitude')
    @classmethod
    def validate_latitude(cls, latitude):
        if latitude > 90 or latitude < -90:
            raise ValueError("Invalid latitude value")
        return latitude
    
    @field_validator('longitude')
    @classmethod
    def validate_longitude(cls, longitude):
        if longitude > 90 or longitude < -90:
            raise ValueError("Invalid longitude value")
        return longitude