import json
import math
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"

class PathInjector:
    '''
    function to add floor name and id into the <svg> tag of each floor image
    '''
    def write_into_svg_tag(self, root, id, name):
        # print([thing for thing in root.attrib.values()])
        
        # required attributes in the <svg> tag
        root.set("data-floor-id", str(id))
        root.set("data-floor-name", str(name))

        # ? lat long values in the db were added for svg and didn't exist in cold version
            # if ne_lat and ne_long and sw_lat and sw_long:
            #     root.set("data-ne-lat", ne_lat)
            #     root.set("data-ne-long", ne_long)
            #     root.set("data-sw-lat", sw_lat)
            #     root.set("data-sw-long", sw_long)


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