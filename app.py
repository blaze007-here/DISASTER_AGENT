import os
import smtplib
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional, TypedDict
import requests
from flask import Flask, jsonify, request, send_from_directory
from langgraph.graph import END, StateGraph

# ======================================================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ======================================================================
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

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

# ======================================================================
# STATE & AGENT LOGIC (LANGGRAPH)
# ======================================================================
class DisasterAgentState(TypedDict):
    latitude: float
    longitude: float
    country: Optional[str]
    user_contact: str
    weather_report: Optional[dict]
    risk_report: Optional[dict]
    emergency_plan: Optional[dict]
    notification_status: Optional[str]
    error: Optional[str]

def get_weather_data(latitude: float, longitude: float) -> dict:
    params = {"lat": latitude, "lon": longitude, "units": "metric", "appid": OPENWEATHER_API_KEY}
    
    # Current Weather
    curr_resp = requests.get(CURRENT_WEATHER_URL, params=params, timeout=10)
    curr_resp.raise_for_status()
    current = curr_resp.json()
    
    # Forecast
    fore_resp = requests.get(FORECAST_URL, params=params, timeout=10)
    fore_resp.raise_for_status()
    forecast = fore_resp.json()

    forecast_entries = []
    now = datetime.now(timezone.utc)
    for item in forecast.get("list", []):
        observed_at = datetime.fromtimestamp(item["dt"], tz=timezone.utc)
        if (observed_at - now).total_seconds() > 24 * 60 * 60:
            break
        forecast_entries.append({
            "time_utc": observed_at.isoformat(),
            "condition": item.get("weather", [{}])[0].get("main", "Unknown"),
            "description": item.get("weather", [{}])[0].get("description", ""),
            "temperature_c": item.get("main", {}).get("temp"),
            "feels_like_c": item.get("main", {}).get("feels_like"),
            "wind_speed_mps": item.get("wind", {}).get("speed", 0),
            "rain_3h_mm": item.get("rain", {}).get("3h", 0),
            "snow_3h_mm": item.get("snow", {}).get("3h", 0),
            "pop": item.get("pop", 0),
        })

    return {
        "location": {
            "name": current.get("name"),
            "country": current.get("sys", {}).get("country"),
            "latitude": current.get("coord", {}).get("lat", latitude),
            "longitude": current.get("coord", {}).get("lon", longitude),
        },
        "current": {
            "observed_at_utc": datetime.fromtimestamp(current["dt"], tz=timezone.utc).isoformat(),
            "condition": current.get("weather", [{}])[0].get("main", "Unknown"),
            "description": current.get("weather", [{}])[0].get("description", ""),
            "temperature_c": current.get("main", {}).get("temp"),
            "feels_like_c": current.get("main", {}).get("feels_like"),
            "humidity_percent": current.get("main", {}).get("humidity"),
            "wind_speed_mps": current.get("wind", {}).get("speed", 0),
            "visibility_m": current.get("visibility"),
        },
        "forecast_24h": forecast_entries,
    }

def detect_weather_alerts(weather_data: dict) -> list[dict]:
    alerts = []
    current = weather_data.get("current", {})
    forecast = weather_data.get("forecast_24h", [])

    if current.get("temperature_c") is not None and current["temperature_c"] >= HEAT_C:
        alerts.append({"type": "heat", "severity": "high", "details": f"Current temperature is {current['temperature_c']}°C."})
    if current.get("temperature_c") is not None and current["temperature_c"] <= COLD_C:
        alerts.append({"type": "cold", "severity": "moderate", "details": f"Current temperature is {current['temperature_c']}°C."})
    if current.get("wind_speed_mps", 0) >= HIGH_WIND_MPS:
        alerts.append({"type": "high_wind", "severity": "high", "details": f"Current wind speed is {current['wind_speed_mps']} m/s."})

    total_rain = sum(entry.get("rain_3h_mm", 0) or 0 for entry in forecast)
    peak_wind = max((entry.get("wind_speed_mps", 0) or 0 for entry in forecast), default=0)
    thunderstorms = [entry for entry in forecast if entry.get("condition") == "Thunderstorm"]

    if total_rain >= HEAVY_RAIN_3H_MM:
        alerts.append({"type": "heavy_rain", "severity": "high", "details": f"{total_rain:.1f} mm of rain forecast in the next 24 hours."})
    if peak_wind >= HIGH_WIND_MPS and current.get("wind_speed_mps", 0) < HIGH_WIND_MPS:
        alerts.append({"type": "high_wind", "severity": "high", "details": f"Wind up to {peak_wind} m/s forecast in the next 24 hours."})
    if thunderstorms:
        alerts.append({"type": "thunderstorm", "severity": "high", "details": "Thunderstorms are forecast in the next 24 hours."})

    return alerts

def assess_risk(weather_report: dict, country: Optional[str] = None) -> dict:
    current = weather_report.get("current", {})
    forecast = weather_report.get("forecast_24h", [])

    temperature_c = current.get("temperature_c") or 0
    current_wind_mps = current.get("wind_speed_mps") or 0
    rain_24h_mm = sum(entry.get("rain_3h_mm", 0) or 0 for entry in forecast)
    peak_wind_mps = max([current_wind_mps] + [entry.get("wind_speed_mps", 0) or 0 for entry in forecast], default=0)
    thunderstorm_forecast = any(entry.get("condition") == "Thunderstorm" for entry in forecast)

    hazards = {
        "heatwave": {"triggered": temperature_c >= HEAT_C, "score": 30 if temperature_c >= HEAT_C else 0},
        "flood": {"triggered": rain_24h_mm >= 50, "score": 35 if rain_24h_mm >= 50 else 0},
        "cyclone": {"triggered": peak_wind_mps >= 17, "score": 35 if peak_wind_mps >= 17 else 0},
        "thunderstorm": {
            "triggered": thunderstorm_forecast or (rain_24h_mm >= 20 and peak_wind_mps >= 10),
            "score": 15 if (thunderstorm_forecast or (rain_24h_mm >= 20 and peak_wind_mps >= 10)) else 0,
        },
    }

    total_score = sum(h["score"] for h in hazards.values())
    if total_score >= 80: risk_level = "severe"
    elif total_score >= 60: risk_level = "high"
    elif total_score >= 30: risk_level = "moderate"
    else: risk_level = "low"

    triggered_hazards = [name for name, h in hazards.items() if h["triggered"]]
    risk_type = ", ".join(triggered_hazards) if triggered_hazards else "none"

    return {
        "risk_level": risk_level,
        "risk_type": risk_type,
        "hazards": hazards,
        "explanation": f"Combined risk score {total_score}/100 ({risk_level}). Hazards: {risk_type}."
    }

def find_nearby_shelters(latitude: float, longitude: float) -> list[dict]:
    params = {"location": f"{latitude},{longitude}", "radius": SHELTER_SEARCH_RADIUS_M, "keyword": SHELTER_KEYWORDS, "key": GOOGLE_MAPS_API_KEY}
    resp = requests.get(PLACES_URL, params=params, timeout=10)
    data = resp.json()
    shelters = []
    for place in data.get("results", [])[:3]:
        loc = place.get("geometry", {}).get("location", {})
        shelters.append({
            "name": place.get("name"),
            "address": place.get("vicinity"),
            "latitude": loc.get("lat"),
            "longitude": loc.get("lng")
        })
    return shelters

def build_emergency_plan(latitude: float, longitude: float, risk_report: dict) -> dict:
    risk_level = str(risk_report.get("risk_level", "low")).lower()
    risk_type = risk_report.get("risk_type", "none")
    triggered_hazards = [h.strip() for h in risk_type.split(",") if h.strip() and h.strip() != "none"]

    steps = []
    for hazard in triggered_hazards:
        steps.extend(PRECAUTIONARY_STEPS.get(hazard, []))
    steps.extend(DEFAULT_PRECAUTIONARY_STEPS)

    if risk_level not in RISK_LEVELS_REQUIRING_PLAN:
        return {"action_required": False, "reason": "Risk level is low. Stay alert.", "shelters": [], "precautionary_steps": steps}

    try:
        shelters = find_nearby_shelters(latitude, longitude)
        return {
            "action_required": True,
            "reason": f"Risk level '{risk_level}' requires immediate action.",
            "shelters": shelters,
            "precautionary_steps": steps
        }
    except Exception as e:
        return {"action_required": True, "reason": f"Action needed but shelter lookup failed: {e}", "shelters": [], "precautionary_steps": steps}

def build_alert_message(state: DisasterAgentState) -> str:
    plan = state["emergency_plan"]
    if not plan.get("action_required"):
        return "Weather Update\n\nCurrent conditions are safe.\nNo evacuation is required."
    lines = ["EMERGENCY WEATHER ALERT", "", plan.get("reason"), ""]
    if plan.get("shelters"):
        lines += ["Nearest Shelter Found:", plan["shelters"][0]["name"], plan["shelters"][0]["address"], ""]
    if plan.get("precautionary_steps"):
        lines.append("Precautionary Steps:")
        lines += [f"- {step}" for step in plan["precautionary_steps"]]
    return "\n".join(lines)

def send_notifications(state: DisasterAgentState) -> str:
    message = build_alert_message(state)
    receiver = state["user_contact"]
    if EMAIL_ADDRESS == "EMAILLL" or receiver == "user@example.com":
        return "Notification mocked (Email configuration missing or using dummy test values)."
    try:
        email = EmailMessage()
        email["From"] = EMAIL_ADDRESS
        email["To"] = receiver
        email["Subject"] = "Emergency Weather Alert System"
        email.set_content(message)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(email)
        return f"Notification successfully emailed to {receiver}"
    except Exception as e:
        return f"Notification failed: {str(e)}"

# ======================================================================
# NODES & GRAPH PIPELINE
# ======================================================================
def weather_node(s):
    try: s["weather_report"] = get_weather_data(s["latitude"], s["longitude"]); s["weather_report"]["alerts"] = detect_weather_alerts(s["weather_report"])
    except Exception as e: s["error"] = f"Weather Error: {e}"
    return s

def risk_node(s):
    if s.get("error"): return s
    s["risk_report"] = assess_risk(s["weather_report"], s.get("country"))
    return s

def plan_node(s):
    if s.get("error"): return s
    s["emergency_plan"] = build_emergency_plan(s["latitude"], s["longitude"], s["risk_report"])
    return s

def notify_node(s):
    if s.get("error"): return s
    s["notification_status"] = send_notifications(s)
    return s

workflow = StateGraph(DisasterAgentState)
workflow.add_node("weather", weather_node)
workflow.add_node("risk", risk_node)
workflow.add_node("plan", plan_node)
workflow.add_node("notify", notify_node)
workflow.set_entry_point("weather")
workflow.add_edge("weather", "risk")
workflow.add_edge("risk", "plan")
workflow.add_edge("plan", "notify")
workflow.add_edge("notify", END)
app_graph = workflow.compile()

# ======================================================================
# FLASK WEB INTERFACE ROUTER
# ======================================================================
app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in {"http://127.0.0.1:5500", "http://localhost:5500"}:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route('/')
def home():
    return send_from_directory(os.path.dirname(__file__), 'index.html')


@app.route('/api/check-disaster', methods=['OPTIONS'])
def check_disaster_options():
    return ('', 204)

@app.route('/api/check-disaster', methods=['POST'])
def check_disaster():
    try:
        data = request.get_json(silent=True) or {}
        initial_state = {
            "latitude": float(data.get("latitude", 28.5764)),
            "longitude": float(data.get("longitude", 77.1868)),
            "country": data.get("country", "India"),
            "user_contact": data.get("email", "user@example.com"),
            "weather_report": None,
            "risk_report": None,
            "emergency_plan": None,
            "notification_status": None,
            "error": None
        }
        final_state = app_graph.invoke(initial_state)
        return jsonify(final_state)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)