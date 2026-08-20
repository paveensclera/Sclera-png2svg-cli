import json
import math
import xml.etree.ElementTree as ET
import base64
import uuid
from datetime import datetime

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace('sclera', 'https://sclera.app/ns')

class PathInjector:
    '''
    function to add floor name and id into the <svg> tag of each floor image
    '''
    def get_dimensions(self, root, fallback_w=2000.0, fallback_h=2000.0):
        viewbox = root.attrib.get("viewBox")
        if viewbox:
            try:
                parts = [float(p) for p in viewbox.replace(",", " ").split() if p.strip()]
                if len(parts) >= 4 and parts[2] > 0 and parts[3] > 0:
                    return float(parts[2]), float(parts[3])
            except Exception:
                pass

        def parse_dim(val, fallback):
            if not val:
                return fallback
            val_str = str(val).strip()
            if val_str.endswith("%"):
                return fallback
            import re
            m = re.match(r"^([\d.]+)", val_str)
            if m:
                try:
                    n = float(m.group(1))
                    if n > 0:
                        return n
                except Exception:
                    pass
            return fallback

        w = parse_dim(root.attrib.get("width"), fallback_w)
        h = parse_dim(root.attrib.get("height"), fallback_h)
        return float(w), float(h)

    def write_into_svg_tag(self, root, id, name, geo_data=None, default_w=2000.0, default_h=2000.0):
        # Ensure viewBox exists before setting width/height to 100%
        if not root.attrib.get("viewBox"):
            width, height = self.get_dimensions(root, default_w, default_h)
            root.set("viewBox", f"0 0 {width} {height}")

        # required attributes in the <svg> tag matching py-png2svg-ms
        root.set("overflow", "visible")
        root.set("width", "100%")
        root.set("height", "100%")
        # till here
        root.set("data-floor-id", str(id) if id is not None else "")
        root.set("data-floor-name", str(name) if name is not None else "Unnamed Floor")
        root.set("data-sclera", "1")
        if geo_data:
            if geo_data.get("neLat") is not None:
                root.set("data-ne-lat", str(geo_data["neLat"]))
            if geo_data.get("neLong") is not None:
                root.set("data-ne-long", str(geo_data["neLong"]))
            if geo_data.get("swLat") is not None:
                root.set("data-sw-lat", str(geo_data["swLat"]))
            if geo_data.get("swLong") is not None:
                root.set("data-sw-long", str(geo_data["swLong"]))
            if geo_data.get("rotationAngle") is not None:
                root.set("data-rotation-angle", str(geo_data["rotationAngle"]))

    def inject_sclera_data(self, root, floor_svg_text, floor_locations, paths_string, geo_data=None):
        width, height = self.get_dimensions(root)

        edit_shapes = []
        detected_areas = []
        locations = []
        created_at_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        if floor_locations is not None:
            sorted_locations = sorted(
                floor_locations,
                key=lambda loc: loc.get("zindex") if loc.get("zindex") is not None else 0
            )

            for location in sorted_locations:
                try:
                    name = location.get("name", "Unnamed Location")
                    area_json = location.get("area")
                    is_removed = location.get("isRemoved", False)
                    location_id = location.get("id")

                    if is_removed or not area_json:
                        continue

                    if location_id:
                        locations.append({
                            "id": str(location_id),
                            "name": str(name)
                        })

                    try:
                        area_data = json.loads(area_json)
                    except (ValueError, TypeError):
                        continue

                    shape = area_data.get("shape", "").lower()
                    if not shape:
                        continue

                    shape_id = str(location_id) if location_id else str(uuid.uuid4())

                    if shape == "rectangle":
                        top_left = area_data["relativeBounds"][0]
                        bottom_right = area_data["relativeBounds"][1]

                        x1 = top_left["relativeLeft"] * width
                        y1 = top_left["relativeTop"] * height
                        x2 = bottom_right["relativeLeft"] * width
                        y2 = bottom_right["relativeTop"] * height

                        edit_shapes.append({
                            "id": shape_id,
                            "type": "rect",
                            "location": str(location_id) if location_id else "",
                            "createdAt": created_at_iso,
                            "connections": [],
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                            "color": "rgba(50,90,180,0.9)",
                            "fill": "rgba(100,149,237,0.35)",
                            "strokeWidth": 2,
                            "style": {
                                "strokeColor": "rgba(50,90,180,0.9)",
                                "fillColor": "rgba(100,149,237,0.35)",
                                "strokeWidth": 2
                            }
                        })

                        detected_areas.append({
                            "id": shape_id,
                            "type": "rect",
                            "x1": top_left["relativeLeft"],
                            "y1": top_left["relativeTop"],
                            "x2": bottom_right["relativeLeft"],
                            "y2": bottom_right["relativeTop"],
                            "color": {
                                "fill": "rgba(100,149,237,0.35)",
                                "border": "rgba(50,90,180,0.9)"
                            },
                            "label": str(name) if name else "Unnamed Location",
                            "userDrawn": True
                        })

                    elif shape == "circle":
                        center = {
                            "x": area_data["relativeCenter"]["relativeLeft"] * width,
                            "y": area_data["relativeCenter"]["relativeTop"] * height,
                        }
                        radius = area_data["relativeRadius"] * min(width, height)

                        edit_shapes.append({
                            "id": shape_id,
                            "type": "circle",
                            "location": str(location_id) if location_id else "",
                            "createdAt": created_at_iso,
                            "connections": [],
                            "cx": center["x"],
                            "cy": center["y"],
                            "r": radius,
                            "color": "rgba(50,90,180,0.9)",
                            "fill": "rgba(100,149,237,0.35)",
                            "strokeWidth": 2,
                            "style": {
                                "strokeColor": "rgba(50,90,180,0.9)",
                                "fillColor": "rgba(100,149,237,0.35)",
                                "strokeWidth": 2
                            }
                        })

                        detected_areas.append({
                            "id": shape_id,
                            "type": "circle",
                            "cx": area_data["relativeCenter"]["relativeLeft"],
                            "cy": area_data["relativeCenter"]["relativeTop"],
                            "r": area_data["relativeRadius"],
                            "color": {
                                "fill": "rgba(100,149,237,0.35)",
                                "border": "rgba(50,90,180,0.9)"
                            },
                            "label": str(name) if name else "Unnamed Location",
                            "userDrawn": True
                        })

                    else:  # Polygon
                        points = [
                            {
                                "x": bound["relativeLeft"] * width,
                                "y": bound["relativeTop"] * height
                            }
                            for bound in area_data["relativeBounds"]
                        ]

                        edit_shapes.append({
                            "id": shape_id,
                            "type": "polygon",
                            "location": str(location_id) if location_id else "",
                            "createdAt": created_at_iso,
                            "connections": [],
                            "points": points,
                            "color": "rgba(50,90,180,0.9)",
                            "fill": "rgba(100,149,237,0.35)",
                            "strokeWidth": 2,
                            "style": {
                                "strokeColor": "rgba(50,90,180,0.9)",
                                "fillColor": "rgba(100,149,237,0.35)",
                                "strokeWidth": 2
                            }
                        })

                        detected_areas.append({
                            "id": shape_id,
                            "type": "polygon",
                            "points": [
                                {
                                    "x": bound["relativeLeft"],
                                    "y": bound["relativeTop"]
                                }
                                for bound in area_data["relativeBounds"]
                            ],
                            "color": {
                                "fill": "rgba(100,149,237,0.35)",
                                "border": "rgba(50,90,180,0.9)"
                            },
                            "label": str(name) if name else "Unnamed Location",
                            "userDrawn": True
                        })

                except Exception as e:
                    print(f"Error packing area in editShapes: {e}")

        door_markers = []
        if paths_string:
            try:
                array_of_paths = json.loads(paths_string)
            except (ValueError, TypeError):
                array_of_paths = []

            for path_item in array_of_paths:
                try:
                    if isinstance(path_item, str):
                        path_item = json.loads(path_item)

                    if isinstance(path_item, dict):
                        # This is a vertical transport marker (door marker)
                        name = path_item.get("name", "")
                        tag = path_item.get("tag", "")
                        vt_type = path_item.get("type", "stairs")
                        rel_pos = path_item.get("relativePosition", {})
                        rel_left = rel_pos.get("relativeLeft", 0.0)
                        rel_top = rel_pos.get("relativeTop", 0.0)
                        
                        x = rel_left * width
                        y = rel_top * height
                        
                        door_markers.append({
                            "id": str(uuid.uuid4()),
                            "x": x,
                            "y": y,
                            "doorType": vt_type,
                            "label": name,
                            "tag": tag
                        })
                    else:
                        # This is a path polyline
                        points = [
                            {
                                "x": point["relativeLeft"] * width,
                                "y": point["relativeTop"] * height
                            }
                            for point in path_item
                        ]

                        edit_shapes.append({
                            "id": str(uuid.uuid4()),
                            "type": "path",
                            "location": "",
                            "createdAt": created_at_iso,
                            "connections": [],
                            "points": points,
                            "color": "#3182CE",
                            "fill": "none",
                            "strokeWidth": 2,
                            "style": {
                                "strokeColor": "#3182CE",
                                "fillColor": "none",
                                "strokeWidth": 2
                            }
                        })

                        # Populate detected_areas for path (normalized coordinates)
                        detected_areas.append({
                            "id": str(uuid.uuid4()),
                            "type": "path",
                            "points": [
                                {
                                    "x": point["relativeLeft"],
                                    "y": point["relativeTop"]
                                }
                                for point in path_item
                            ],
                            "color": {
                                "fill": "rgba(49, 130, 206, 0.00)",
                                "border": "rgba(49, 130, 206, 0.90)"
                            },
                            "label": "",
                            "userDrawn": True
                        })

                except Exception as e:
                    print(f"Error packing marker/polyline in editShapes: {e}")

        state_obj = {
            "walls": [],
            "cameras": [],
            "bleBeacons": [],
            "bleReceivers": [],
            "wifiAPs": [],
            "measNodes": [],
            "measSegs": [],
            "measScale": None,
            "measScaleUnit": "ft",
            "measVisible": True,
            "detectedAreas": detected_areas,
            "editShapes": edit_shapes,
            "bgImage": None,
            "floorSvgText": floor_svg_text,
            "transform": {"x": 0, "y": 0, "scale": 1},
            "defaultCameraType": "bullet",
            "wallsGlobalLocked": False,
            "cameraRayVis": True,
            "beaconWaveVis": True,
            "wifiWaveVis": True,
            "doorMarkers": door_markers,
            "locations": locations,
            "geoData": geo_data
        }

        raw = json.dumps(state_obj, ensure_ascii=False)
        encoded_base64 = base64.b64encode(raw.encode('utf-8')).decode('ascii')

        metadata_el = ET.Element("metadata")
        data_el = ET.SubElement(metadata_el, "{https://sclera.app/ns}data")
        data_el.text = encoded_base64
        root.insert(0, metadata_el)


    '''
    function to create new <path> tags from the area column of db
    converted to Python from JavaScript logic of handling generated_data.json
    (refer React-Pathfinder)
    '''
    def write_location_path_tags(self, root, floor_locations):
        width, height = self.get_dimensions(root)
        if not root.attrib.get("viewBox"):
            root.set("viewBox", f"0 0 {width} {height}")

        new_paths = []

        if floor_locations is not None:
            # Sort by z-index if available
            floor_locations = sorted(
                floor_locations,
                key=lambda loc: loc.get("zindex") if loc.get("zindex") is not None else 0
            )

            for location in floor_locations:
                try:
                    name = location.get("name", "Unnamed Location")
                    area_json = location.get("area")
                    is_removed = location.get("isRemoved", False)
                    location_id = location.get("id")
                    zindex = location.get("zindex", 0)

                    if is_removed or not area_json:
                        continue

                    try:
                        area_data = json.loads(area_json)
                    except (ValueError, TypeError) as e:
                        print(f"Error parsing area JSON for location {name}: {e}")
                        continue

                    shape = area_data.get("shape", "").lower()
                    if not shape:
                        continue

                    path_data = ""
                    points = []
                    cx, cy = 0.0, 0.0
                    shape_type_attr = ""

                    if shape == "rectangle":
                        top_left = area_data["relativeBounds"][0]
                        bottom_right = area_data["relativeBounds"][1]

                        x1 = top_left["relativeLeft"] * width
                        y1 = top_left["relativeTop"] * height
                        x2 = bottom_right["relativeLeft"] * width
                        y2 = bottom_right["relativeTop"] * height

                        x = x1
                        y = y1
                        W = x2 - x1
                        H = y2 - y1

                        path_data = f"M {x},{y} h {W} v {H} h {-W} Z"
                        cx = (x1 + x2) / 2.0
                        cy = (y1 + y2) / 2.0
                        shape_type_attr = "rectangle"

                    elif shape == "circle":
                        center = {
                            "x": area_data["relativeCenter"]["relativeLeft"] * width,
                            "y": area_data["relativeCenter"]["relativeTop"] * height,
                        }
                        radius = area_data["relativeRadius"] * min(width, height)

                        cx = center["x"]
                        cy = center["y"]
                        r = radius

                        path_data = f"M {cx - r},{cy} a {r},{r} 0 1,0 {2*r},0 a {r},{r} 0 1,0 {-2*r},0 Z"
                        shape_type_attr = "circle"

                    else:  # Polygon
                        points = [
                            {
                                "x": bound["relativeLeft"] * width,
                                "y": bound["relativeTop"] * height
                            }
                            for bound in area_data["relativeBounds"]
                        ]

                        path_data = " ".join(
                            f"{'M' if i == 0 else 'L'} {p['x']},{p['y']}"
                            for i, p in enumerate(points)
                        ) + " Z"

                        cx = sum(p["x"] for p in points) / len(points)
                        cy = sum(p["y"] for p in points) / len(points)
                        shape_type_attr = "polygon"

                    if path_data:
                        path_element = ET.Element(f"{{{SVG_NS}}}path", {
                            "d": path_data,
                            "fill": "rgba(100,149,237,0.35)",
                            "stroke": "rgba(50,90,180,0.9)",
                            "stroke-width": "2",
                            "data-id": str(location_id) if location_id is not None else "",
                            "data-shape-type": shape_type_attr,
                            "data-user-drawn": "true",
                            "data-location": str(name) if name else "Unnamed Location"
                        })
                        new_paths.append(path_element)

                except (TypeError, KeyError, IndexError) as e:
                    print(f"Error processing location {location.get('name', 'Unknown')}: {e}")

            for path in new_paths:
                root.append(path)


    '''
    function to create new <path> tag from path column of db
    converted to Python from JavaScript logic of handling generated_paths.json
    '''
    def write_path_path_tags(self, root, array_of_strings):

        if not array_of_strings:
            return
        
        try:
            array_of_paths = json.loads(array_of_strings)
        except (ValueError, TypeError) as e:
            print(f"Error parsing paths JSON: {e}")
            return
        
        width, height = self.get_dimensions(root)
        if not root.attrib.get("viewBox"):
            root.set("viewBox", f"0 0 {width} {height}")

        new_paths = []

        VT_EXPORT_COLORS = {
            "stairs": "#21573e",
            "elevator": "#754a12",
            "escalator": "#1a2754",
            "building_exit": "#055a75",
            "skyway": "#055a75"
        }
        VT_EXPORT_LABELS = {
            "stairs": "Stairs",
            "elevator": "Elevator",
            "escalator": "Escalator",
            "building_exit": "Bldg Exit",
            "skyway": "Skyway"
        }

        # Calculate sizes based on viewport dimensions to match exportUtils.js
        camR = max(width, height) * 0.015
        r = camR * 0.8
        font_size = camR * 0.7
        label_font_size = camR * 0.75
        dy = r + camR * 0.85

        path_counter = 0

        if array_of_paths is not None:
            for path_item in array_of_paths:
                try:
                    if isinstance(path_item, str):
                        path_item = json.loads(path_item)

                    if isinstance(path_item, dict):
                        # This is a vertical transport marker (door marker)
                        name = path_item.get("name", "")
                        tag = path_item.get("tag", "")
                        vt_type = path_item.get("type", "stairs")
                        rel_pos = path_item.get("relativePosition", {})
                        rel_left = rel_pos.get("relativeLeft", 0.0)
                        rel_top = rel_pos.get("relativeTop", 0.0)
                        
                        x = rel_left * width
                        y = rel_top * height
                        
                        color = VT_EXPORT_COLORS.get(vt_type, "#4A90D9")
                        type_label = VT_EXPORT_LABELS.get(vt_type, "VT")
                        
                        # Create <g> element for vt_marker
                        g_element = ET.Element(f"{{{SVG_NS}}}g", {
                            "data-type": "vt_marker",
                            "data-vt-type": vt_type,
                            "data-label": name,
                            "data-tag": tag,
                            "data-relative-left": f"{rel_left:.6f}",
                            "data-relative-top": f"{rel_top:.6f}",
                            "transform": f"translate({x:.2f},{y:.2f})"
                        })
                        
                        # Circle child
                        ET.SubElement(g_element, f"{{{SVG_NS}}}circle", {
                            "r": f"{r:.2f}",
                            "fill": color,
                            "opacity": "0.75"
                        })
                        
                        # Text child for label abbreviation
                        txt_el = ET.SubElement(g_element, f"{{{SVG_NS}}}text", {
                            "font-size": f"{font_size:.2f}",
                            "text-anchor": "middle",
                            "dominant-baseline": "central",
                            "font-family": "monospace",
                            "fill": "white",
                            "font-weight": "bold"
                        })
                        txt_el.text = type_label
                        new_paths.append(g_element)
                    else:
                        # Convert relative coordinates to absolute coordinates
                        points = [
                            {
                                "x": point["relativeLeft"] * width,
                                "y": point["relativeTop"] * height
                            }
                            for point in path_item
                        ]

                        # Generate SVG path data string for polyline
                        path_data = " ".join(
                            f"{'M' if i == 0 else 'L'} {p['x']},{p['y']}"
                            for i, p in enumerate(points)
                        )

                        # Create <path> tag with attributes
                        path_element = ET.Element(f"{{{SVG_NS}}}path", {
                            "d": path_data,
                            "fill": "none",
                            "stroke": "rgba(49, 130, 206, 0.9)",
                            "stroke-width": "2",
                            "data-shape-type": "polyline",
                            "data-user-drawn": "true",
                            "data-path-id": f"user-{path_counter}",
                        })
                        path_counter += 1

                        new_paths.append(path_element)

                except Exception as e:
                    print(f"Error packing marker/polyline in write_path_path_tags: {e}")
                    pass

            # Append all elements to the root
            for path in new_paths:
                root.append(path)

        return root