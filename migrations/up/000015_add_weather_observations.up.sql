CREATE TABLE IF NOT EXISTS weather_observations (
    weather_observation_id SERIAL PRIMARY KEY,
    camera_id INTEGER REFERENCES cameras(camera_id),
    observed_at TIMESTAMTZ NOT NULL DEFAULT NOW(),
    temperature FLOAT,
    precipitation FLOAT
);