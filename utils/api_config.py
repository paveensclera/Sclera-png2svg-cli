import os
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

load_dotenv()


class Data:
    def __init__(self):
        mode = (os.getenv("MODE") or "").lower()
        if mode == "multi-tenant":
            raw_base = os.getenv("MULTI_TENANT_BASEURL") or os.getenv("BASE_URL") or ""
            self.base_url = f"{raw_base.rstrip('/')}/api/v1/migration"
        else:
            self.base_url = os.getenv("BASE_URL")
        self.session = requests.Session()
        self.cached_geo_data = {}

        # Configure retry strategy to handle transient connection issues
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get_floor(self, floor_id, vdms_id=None):
        if not self.base_url:
            return None
        url = f"{self.base_url}/floor/name/{floor_id}"
        if vdms_id:
            url += f"?vdms_id={vdms_id}"
        print(f"Fetching floor name from URL: {url}")  # Debugging line
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()

            import json

            try:
                data_dict = response.json()
                floor_name = data_dict.get("name")

                initial_position = data_dict.get("initialPosition")
                if initial_position:
                    if isinstance(initial_position, str):
                        try:
                            coords = json.loads(initial_position)
                        except Exception:
                            coords = None
                    else:
                        coords = initial_position

                    if isinstance(coords, list) and len(coords) >= 2:
                        p1 = coords[0]  # NW corner: [nwLat, nwLong]
                        p2 = coords[1]  # SE corner: [seLat, seLong]
                        if (
                            isinstance(p1, list)
                            and len(p1) >= 2
                            and isinstance(p2, list)
                            and len(p2) >= 2
                        ):
                            nw_lat = float(p1[0])
                            nw_long = float(p1[1])
                            se_lat = float(p2[0])
                            se_long = float(p2[1])

                            angle_val = data_dict.get("angle")
                            try:
                                angle_int = (
                                    int(angle_val) if angle_val is not None else 0
                                )
                            except (ValueError, TypeError):
                                angle_int = 0

                            self.cached_geo_data[floor_id] = {
                                "swLat": se_lat,
                                "swLong": nw_long,
                                "neLat": nw_lat,
                                "neLong": se_long,
                                "rotationAngle": angle_int,
                            }
                return floor_name
            except Exception:
                # Fallback to returning raw text if it's not JSON
                return response.text
        except Exception as e:
            print(f"Error fetching floor name for floor {floor_id}: {e}")
            return None

    def get_locations(self, floor_id, vdms_id=None):
        if not self.base_url:
            return None
        url = f"{self.base_url}/location/{floor_id}"
        if vdms_id:
            url += f"?vdms_id={vdms_id}"

        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            formatted = [
                {
                    "name": item.get("name"),
                    "area": item.get("area"),
                    "isRemoved": (
                        item.get("is_removed")
                        if item.get("is_removed") is not None
                        else item.get("isRemoved", False)
                    ),
                    "zindex": (
                        item.get("z_index")
                        if item.get("z_index") is not None
                        else item.get("zindex", 0)
                    ),
                    "id": item.get("location_id") or item.get("id"),
                }
                for item in data
            ]

            return formatted if formatted else None

        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"Error fetching locations for floor {floor_id}: {e}")
            return None

    def get_paths(self, floor_id, vdms_id=None):
        if not self.base_url:
            return None
        url = f"{self.base_url}/path/{floor_id}"
        if vdms_id:
            url += f"?vdms_id={vdms_id}"

        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.text

        except requests.exceptions.RequestException as e:
            print(f"Error fetching paths for floor {floor_id}: {e}")
            return None

    def get_geo_data(self, floor_id):
        # Return cached geo data if populated from get_floor
        if hasattr(self, "cached_geo_data") and floor_id in self.cached_geo_data:
            return self.cached_geo_data[floor_id]

        if not self.base_url:
            return None
        sclera_base_url = self.base_url.replace("/migration", "")
        url = f"{sclera_base_url}/floor/{floor_id}"

        try:
            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                return None

            import xml.etree.ElementTree as ET
            import base64
            import json

            root = ET.fromstring(response.content)

            geo_data = None
            metadata = root.find("{http://www.w3.org/2000/svg}metadata")
            if metadata is not None:
                sclera_data = metadata.find("{https://sclera.app/ns}data")
                if sclera_data is not None and sclera_data.text:
                    try:
                        decoded = base64.b64decode(sclera_data.text).decode("utf-8")
                        state = json.loads(decoded)
                        geo_data = state.get("geoData")
                    except Exception as e:
                        print(f"Error parsing metadata geoData: {e}")

            if not geo_data:
                ne_lat = root.attrib.get("data-ne-lat")
                ne_long = root.attrib.get("data-ne-long")
                sw_lat = root.attrib.get("data-sw-lat")
                sw_long = root.attrib.get("data-sw-long")
                rot_angle = root.attrib.get("data-rotation-angle")

                if ne_lat and ne_long and sw_lat and sw_long:
                    try:
                        geo_data = {
                            "neLat": float(ne_lat),
                            "neLong": float(ne_long),
                            "swLat": float(sw_lat),
                            "swLong": float(sw_long),
                            "rotationAngle": float(rot_angle) if rot_angle else 0.0,
                        }
                    except (ValueError, TypeError):
                        pass

            return geo_data

        except Exception as e:
            print(f"Error fetching/parsing geo data for floor {floor_id}: {e}")
            return None

    def upload_floor(self, floor_id, svg_bytes):
        url = f"{self.base_url}/upload"

        files = {"file": (floor_id, svg_bytes, "image/svg+xml")}
        response = self.session.post(url, files=files)
        response.raise_for_status()
        return response.text
