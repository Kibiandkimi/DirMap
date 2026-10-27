"""Printed routes land on the course overlay exactly where the phone draws them."""
import math

import cv2
import numpy as np
import pytest

from dirmap import printing


def lnglat(x, y):
    return [math.degrees(x/printing.R), math.degrees(2*math.atan(math.exp(y/printing.R))-math.pi/2)]


def course(tmp_path):
    overlay = np.zeros((100, 200, 4), np.uint8)
    overlay[:, :150] = (230, 240, 235, 255)  # the map; the rest is transparent
    path = tmp_path/'overlay.png'
    cv2.imwrite(str(path), overlay)
    # Mercator extent 1000 x 500 m, so one pixel is 5 m.
    release = {'extent': [0, 0, 1000, 500],
               'controls': [{'id': 'c1', 'label': '31', 'lnglat': lnglat(500, 250)}, {'id': 'c2', 'label': '32'}],
               'plan': {'routes': [{'teamId': 'A', 'controlIds': ['c1', 'c2'], 'geometry': [lnglat(100, 250), lnglat(900, 250)]},
                                   {'teamId': 'B', 'controlIds': ['c1'], 'geometry': [lnglat(250, 450), lnglat(250, 50)]}]}}
    return release, path


def test_route_is_drawn_in_its_team_colour_at_the_projected_pixels(tmp_path):
    release, path = course(tmp_path)
    image = cv2.imdecode(np.frombuffer(printing.route_map(release, 'B', path), np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == (100, 200, 3)
    blue = np.array([219, 110, 65])  # '#416edb' in BGR, the second team's colour
    assert np.abs(image[50, 50].astype(int)-blue).max() < 60
    assert np.abs(image[85, 50].astype(int)-blue).max() < 60
    # The transparent part of the overlay prints as paper, not black.
    assert image[5, 195].min() > 200
    first = cv2.imdecode(np.frombuffer(printing.route_map(release, 'A', path), np.uint8), cv2.IMREAD_COLOR)
    assert np.abs(first[50, 60].astype(int)-np.array([63, 105, 236])).max() < 60


def test_unknown_team_or_missing_overlay_is_refused(tmp_path):
    release, path = course(tmp_path)
    with pytest.raises(ValueError, match='没有这个队伍'):
        printing.route_map(release, 'C', path)
    with pytest.raises(ValueError, match='原图不可用'):
        printing.route_map(release, 'A', tmp_path/'missing.png')
