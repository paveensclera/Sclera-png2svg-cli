import os
from dotenv import load_dotenv
import requests

load_dotenv()

class Data:
    def __init__(self):
        self.base_url = os.getenv("BASE_URL")

    def get_floor(self, floor_id):
        url = f"{self.base_url}/floor/name/{floor_id}"

        response = requests.get(url)
        return response.text
        

    def get_locations(self, floor_id):
        url = f"{self.base_url}/location/{floor_id}"

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()

            # Return dicts normalized to expected field names
            formatted = [
                {
                    "name": item.get("name"),
                    "area": item.get("area"),
                    "isRemoved": item.get("isRemoved"),
                    "zindex": item.get("zindex"),
                    "id": item.get("id")
                }
                for item in data
            ]

            return formatted if formatted else None
        
        except requests.exceptions.RequestException as e:
            print(f"Error fetching locations for floor {floor_id}: {e}")
            return None


    def get_paths(self, floor_id):
        url = f"{self.base_url}/path/{floor_id}"

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.text
        
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching paths for floor {floor_id}: {e}")
            return None


    def upload_floor(self, floor_id, svg_bytes):
        url = f"{self.base_url}/upload"

        files = {
            "file": (floor_id, svg_bytes, "image/svg+xml")
        }
        response = requests.post(url, files=files)
        response.raise_for_status()
        return response.text