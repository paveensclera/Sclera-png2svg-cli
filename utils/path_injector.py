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
    def write_into_svg_tag(self, root, id, name):
        # required attributes in the <svg> tag
        root.set("data-floor-id", str(id))
        root.set("data-floor-name", str(name))
        root.set("data-sclera", "1")

    def inject_sclera_data(self, root, floor_svg_text, floor_locations, paths_string):
        viewbox = root.attrib.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                width = float(parts[2])
                height = float(parts[3])
            else:
                width = float(root.attrib.get("width", 2000))
                height = float(root.attrib.get("height", 2000))
        else:
            width = float(root.attrib.get("width", 2000))
            height = float(root.attrib.get("height", 2000))

        edit_shapes = []
        locations = []
        created_at_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        if floor_locations is not None:
            sorted_locations = sorted(
                floor_locations,
                key=lambda loc: loc.get("zindex", 0)
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

                except Exception as e:
                    print(f"Error packing area in editShapes: {e}")

        if paths_string:
            try:
                array_of_paths = json.loads(paths_string)
            except (ValueError, TypeError):
                array_of_paths = []

            for path_points in array_of_paths:
                try:
                    if isinstance(path_points, str):
                        path_points = json.loads(path_points)

                    points = [
                        {
                            "x": point["relativeLeft"] * width,
                            "y": point["relativeTop"] * height
                        }
                        for point in path_points
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
                except Exception as e:
                    print(f"Error packing route in editShapes: {e}")

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
            "detectedAreas": [],
            "editShapes": edit_shapes,
            "bgImage": None,
            "floorSvgText": floor_svg_text,
            "transform": {"x": 0, "y": 0, "scale": 1},
            "defaultCameraType": "bullet",
            "wallsGlobalLocked": False,
            "cameraRayVis": True,
            "beaconWaveVis": True,
            "wifiWaveVis": True,
            "doorMarkers": [],
            "locations": locations,
            "geoData": None
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
        viewbox = root.attrib.get("viewBox")
        if viewbox:
            _, _, width, height = map(float, viewbox.split())
        else:
            width = float(root.attrib.get("width", 0))
            height = float(root.attrib.get("height", 0))

            if width == 0 or height == 0:
                raise ValueError("SVG missing both viewBox and width/height")

            root.set("viewBox", f"0 0 {width} {height}")



        new_paths = []

        if floor_locations is not None:
            # Sort by z-index if available
            floor_locations = sorted(
                floor_locations,
                key=lambda loc: loc.get("zindex", 0)
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

                    if shape == "rectangle":
                        top_left = area_data["relativeBounds"][0]
                        bottom_right = area_data["relativeBounds"][1]

                        x1 = top_left["relativeLeft"] * width
                        y1 = top_left["relativeTop"] * height
                        x2 = bottom_right["relativeLeft"] * width
                        y2 = bottom_right["relativeTop"] * height

                        points = [
                            {"x": x1, "y": y1},
                            {"x": x2, "y": y1},
                            {"x": x2, "y": y2},
                            {"x": x1, "y": y2},
                        ]

                    elif shape == "circle":
                        num_points = 8
                        center = {
                            "x": area_data["relativeCenter"]["relativeLeft"] * width,
                            "y": area_data["relativeCenter"]["relativeTop"] * height,
                        }
                        radius = area_data["relativeRadius"] * min(width, height)
                        path_data = (
                            f"M {center['x'] - radius} {center['y']} "
                            f"A {radius} {radius} 0 1 0 {center['x'] + radius} {center['y']} "
                            f"A {radius} {radius} 0 1 0 {center['x'] - radius} {center['y']} Z"
                        )
                        points = [
                            {
                                "x": center["x"] + radius * math.cos(2 * math.pi * i / num_points),
                                "y": center["y"] + radius * math.sin(2 * math.pi * i / num_points),
                            }
                            for i in range(num_points)
                        ]

                    else:  # Polygon
                        points = [
                            {
                                "x": bound["relativeLeft"] * width,
                                "y": bound["relativeTop"] * height
                            }
                            for bound in area_data["relativeBounds"]
                        ]

                    if points:
                        path_data = " ".join(
                            f"{'M' if i == 0 else 'L'} {p['x']} {p['y']}"
                            for i, p in enumerate(points)
                        ) + " Z"

                        path_element = ET.Element(f"{{{SVG_NS}}}path", {
                            "d": path_data,
                            "stroke": "#3182ce",
                            "stroke-width": "2",
                            "stroke-linecap": "round",
                            "stroke-linejoin": "round",
                            "fill": "#3182ce",
                            "fill-opacity": "0.5",
                            "data-user-drawn": "true",
                            "data-points": json.dumps(points),
                            "data-shape-type": shape,
                            "data-color": "#3182ce",
                            "data-stroke-width": "2",
                            "data-location": str(name) if name else "Unnamed Location",
                            "data-id": str(location_id),
                            "data-z-index": str(zindex),
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
        
        # Extract viewBox dimensions
        viewbox = root.attrib.get("viewBox")

        if viewbox:
            _, _, width, height = map(float, viewbox.split())
        else:
            width = float(root.attrib.get("width", 0))
            height = float(root.attrib.get("height", 0))

            if width == 0 or height == 0:
                raise ValueError("SVG missing both viewBox and width/height")

            root.set("viewBox", f"0 0 {width} {height}")

        new_paths = []

        if array_of_paths is not None:
            for path_points in array_of_paths:
                try:
                    if isinstance(path_points, str):
                        path_points = json.loads(path_points)

                    # Convert relative coordinates to absolute coordinates
                    points = [
                        {
                            "x": point["relativeLeft"] * width,
                            "y": point["relativeTop"] * height
                        }
                        for point in path_points
                    ]

                    # Generate SVG path data string for polyline (no 'Z' since polylines are not closed)
                    path_data = " ".join(
                        f"{'M' if i == 0 else 'L'} {p['x']} {p['y']}"
                        for i, p in enumerate(points)
                    )

                    # Create <path> tag with attributes
                    path_element = ET.Element(f"{{{SVG_NS}}}path", {

                        "d": path_data,
                        "stroke": "#3182ce",
                        "stroke-width": "2",
                        "stroke-linecap": "round",
                        "stroke-linejoin": "round",
                        "fill": "none",
                        "data-user-drawn": "true",
                        # "data-path-id": "2",
                        "data-points": json.dumps(points),
                        "data-shape-type": "polyline",
                        "data-color": "#3182ce",
                        "data-stroke-width": "2",
                    })

                    new_paths.append(path_element)

                except Exception as e:
                    # Optionally log the error for debugging
                    # print(f"Error processing polyline: {e}")
                    pass

            # Append all path elements to the root
            for path in new_paths:
                root.append(path)

        return root  # Optional: return root for chaining or further processing