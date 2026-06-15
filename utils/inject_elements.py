import numpy as np
import xml.etree.ElementTree as ET

def inject_elements(svg, raw_svg_content, detections, floor_id, floor_name = None, floor_locations = None, paths = None, injector = None):
    """
    Injects text elements into an SVG file based on detected text regions.
    """
    ET.register_namespace('', "http://www.w3.org/2000/svg")
    ET.register_namespace('sclera', 'https://sclera.app/ns')
    root = ET.fromstring(svg)
    
    # remove extra vtracer path
    for child in list(root):
        tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag_name == 'path' and 'd' in child.attrib:
            d = child.attrib['d']
            if d.startswith('M0,0') or d.startswith('M 0 0'):
                root.remove(child)
                break
    
    # inject floor metadata
    if injector:
        injector.write_into_svg_tag(root, floor_id, floor_name)
        injector.write_location_path_tags(root, floor_locations)
        injector.write_path_path_tags(root, paths)
        injector.inject_sclera_data(root, raw_svg_content, floor_locations, paths)
    
    # inject text elements for detected text regions
    if detections:
        font_size = np.median([det["font_size"] for det in detections]) if detections else 25
        
        for det in detections:
            poly = np.array(det["poly"], dtype=np.float64)
            cx, cy = np.mean(poly, axis=0)
            
            avg_horiz = (np.linalg.norm(poly[1] - poly[0]) + np.linalg.norm(poly[3] - poly[2])) / 2.0
            avg_vert = (np.linalg.norm(poly[0] - poly[3]) + np.linalg.norm(poly[2] - poly[1])) / 2.0
            
            if avg_horiz >= avg_vert:
                dx, dy = poly[1][0] - poly[0][0], poly[1][1] - poly[0][1]
            else:
                dx, dy = poly[0][0] - poly[3][0], poly[0][1] - poly[3][1]
            
            angle_deg = np.degrees(np.arctan2(dy, dx))
        
            attrs = {
                "x": f"{cx:.1f}", 
                "y": f"{cy:.1f}",
                "font-size": f"{font_size - 4}px", 
                "fill": "black",
                "text-anchor": "middle", "dominant-baseline": "central",
                "font-family": "JetBrainsMono Nerd Font",
            }
            
            if abs(angle_deg) > 10.0:
                attrs["transform"] = f"rotate({angle_deg:.2f}, {cx:.1f}, {cy:.1f})"
            
            text_el = ET.SubElement(root, "text", attrs)
            text_el.text = det["text"]
            
    return ET.ElementTree(root)