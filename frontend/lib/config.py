import os

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
API_WS_URL = os.getenv("API_WS_URL", "ws://localhost:8000/api/v1/ws/realtime")
SAMPLES_DIR = os.getenv("SAMPLES_DIR", "/app/samples")

# Time zones an admin picks from on the settings page, by plant. The API speaks UTC; every
# station shows and enters times in the zone the admin chose (any IANA zone is accepted).
PLANT_ZONES = {
    "Asia/Taipei": "Taiwan",
    "America/Ciudad_Juarez": "Mexico (Ciudad Juárez)",
    "Asia/Kuala_Lumpur": "Malaysia",
    "UTC": "UTC",
}

UNITS = {"chamber_temp": "°C", "cpu_temp": "°C", "gpu_temp": "°C", "dimm_temp": "°C", "nvme_temp": "°C",
         "power_w": "W", "fan_rpm": "RPM"}
LABELS = {"chamber_temp": "Chamber temp", "cpu_temp": "CPU temp", "gpu_temp": "GPU temp", "dimm_temp": "Memory temp",
          "nvme_temp": "SSD temp", "power_w": "Power", "fan_rpm": "Fan speed"}
