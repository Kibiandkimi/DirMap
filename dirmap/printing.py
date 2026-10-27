"""Printable team route over the frozen course overlay.

The image only carries geometry and digits: the server may lack CJK fonts,
so titles and the stop list are laid out by the browser around it.
"""
import math

import cv2
import numpy as np

R = 6378137.
# Same order as teamColors in web/src/map.ts, so paper matches the phone.
TEAM_COLOURS = ['#ec693f', '#416edb', '#9370ca', '#159386', '#bb8426', '#d95492', '#56974e', '#a5513f', '#54869f', '#887760']


def _pixels(lnglat, extent, size):
    lnglat = np.asarray(lnglat, float).reshape(-1, 2)
    x = np.radians(lnglat[:, 0])*R
    y = np.log(np.tan(np.pi/4+np.radians(lnglat[:, 1])/2))*R
    width, height = size
    return np.column_stack(((x-extent[0])/(extent[2]-extent[0])*width, (extent[3]-y)/(extent[3]-extent[1])*height))


def _colour(hex_colour):
    value = hex_colour.lstrip('#')
    return tuple(int(value[i:i+2], 16) for i in (4, 2, 0))


def _label(image, text, centre, scale, colour):
    font, thickness = cv2.FONT_HERSHEY_DUPLEX, max(1, round(scale*2))
    (width, height), _ = cv2.getTextSize(text, font, scale, thickness)
    origin = (int(centre[0]-width/2), int(centre[1]+height/2))
    cv2.putText(image, text, origin, font, scale, (255, 255, 255), thickness+max(2, round(scale*4)), cv2.LINE_AA)
    cv2.putText(image, text, origin, font, scale, colour, thickness, cv2.LINE_AA)


def route_map(release, team_id, overlay_path):
    """JPEG bytes: the team's route, numbered stops, start and finish."""
    routes = release['plan']['routes']
    index = next((i for i, r in enumerate(routes) if r['teamId'] == team_id), None)
    if index is None:
        raise ValueError('课程中没有这个队伍')
    route, colour = routes[index], TEAM_COLOURS[index % len(TEAM_COLOURS)]
    overlay = cv2.imread(str(overlay_path), cv2.IMREAD_UNCHANGED)
    if overlay is None or overlay.ndim != 3 or overlay.shape[2] != 4:
        raise ValueError('课程原图不可用')
    alpha = overlay[:, :, 3:4].astype(np.float32)/255
    image = (overlay[:, :, :3].astype(np.float32)*alpha+255*(1-alpha)).astype(np.uint8)
    height, width = image.shape[:2]
    unit = max(width, height)/1000
    extent = release['extent']
    line = np.rint(_pixels(route['geometry'], extent, (width, height))).astype(np.int32)
    team = _colour(colour)
    # A translucent band keeps the printed map's own symbols readable.
    band = image.copy()
    cv2.polylines(band, [line], False, team, max(2, round(unit*3.2)), cv2.LINE_AA)
    image = cv2.addWeighted(band, .55, image, .45, 0)
    cv2.polylines(image, [line], False, team, max(1, round(unit*.9)), cv2.LINE_AA)
    start, finish = line[0], line[-1]
    side = unit*14
    triangle = np.array([[start[0], start[1]-side*.66], [start[0]-side*.58, start[1]+side*.33],
                         [start[0]+side*.58, start[1]+side*.33]], np.int32)
    cv2.polylines(image, [triangle], True, (165, 35, 190), max(2, round(unit*2)), cv2.LINE_AA)
    for radius in (unit*7, unit*10):
        cv2.circle(image, tuple(map(int, finish)), int(radius), (165, 35, 190), max(2, round(unit*2)), cv2.LINE_AA)
    controls = {c['id']: c for c in release['controls'] if c.get('lnglat')}
    for order, identifier in enumerate(route['controlIds'], 1):
        control = controls.get(identifier)
        if control is None:
            continue
        x, y = _pixels(control['lnglat'], extent, (width, height))[0]
        cv2.circle(image, (int(x), int(y)), int(unit*11), team, max(2, round(unit*2.2)), cv2.LINE_AA)
        # The sequence number sits outside the ring, away from the map's
        # own control number printed inside or beside the circle.
        angle = math.radians(-45)
        _label(image, str(order), (x+math.cos(angle)*unit*27, y+math.sin(angle)*unit*27), unit*.95, team)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError('无法生成路线图')
    return encoded.tobytes()
