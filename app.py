import os
import smtplib
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional, TypedDict
import requests
from flask import Flask, jsonify, request, send_from_directory
from langgraph.graph import END, StateGraph


def load_local_env_file() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_local_env_file()

                                                                        
                                       
                                                                        
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()

CURRENT_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"

HEAVY_RAIN_3H_MM = 15.0
HIGH_WIND_MPS = 15.0
HEAT_C = 40.0
COLD_C = 5.0    
SHELTER_SEARCH_RADIUS_M = 5000
SHELTER_KEYWORDS = "emergency shelter OR evacuation center OR community center OR school"
RISK_LEVELS_REQUIRING_PLAN = {"moderate", "high", "severe"}

PRECAUTIONARY_STEPS = {
    "flood": ["Move to higher ground and avoid walking or driving through flood water.", "Unplug electrical appliances before leaving."],
    "cyclone": ["Secure loose outdoor objects and stay away from windows.", "Keep a battery-powered radio for updates if power is lost."],
    "heatwave": ["Stay hydrated and avoid outdoor activity during peak heat hours.", "Check on elderly or vulnerable neighbors."],
    "thunderstorm": ["Stay indoors and avoid open fields or tall isolated trees.", "Unplug sensitive electronics during lightning activity."]
}
DEFAULT_PRECAUTIONARY_STEPS = [
    "Pack essential medications, identification documents, and water.",
    "Follow instructions issued by local emergency authorities."
]


def build_fallback_weather_data(latitude: float, longitude: float) -> dict:
    observed_at = datetime.now(timezone.utc)
    return {
        "location": {
            "name": "Demo Location",
            "country": "IN",
            "latitude": latitude,
            "longitude": longitude,
        },
        "current": {
            "observed_at_utc": observed_at.isoformat(),
            "condition": "Clear",
            "description": "demo weather data used because OpenWeather is unavailable",
            "temperature_c": 28.0,
            "feels_like_c": 29.0,
            "humidity_percent": 55,
            "wind_speed_mps": 3.5,
            "visibility_m": 10000,
        },
        "forecast_24h": [],
        "source": "demo",
    }

                                                                        
                                 
                                                                        
class DisasterAgentState(TypedDict):
    latitude: float
    longitude: float
    country: Optional[str]
    user_contact: str
    weather_report: Optional[dict]
