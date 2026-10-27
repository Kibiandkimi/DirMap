"""Independent square-intersection checks for additional routing directions."""
import numpy as np
import pytest
from scipy.ndimage import label

from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph


def registration(width, height):
    source = [[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]]
    affine = [[1, 0, 0], [0, -1, height-1]]
    return Registration(affine, source, affine_points(affine, source))


def new_edges(graph):
    row, col = graph.weights.nonzero()
    delta = np.abs(graph.pixels[row]-graph.pixels[col])
    size = graph.cell_sizes[row]
    keep = (row < col) & (size == graph.cell_sizes[col])
    keep &= ((delta[:, 0] == 2*size) & (delta[:, 1] == size)) | ((delta[:, 1] == 2*size) & (delta[:, 0] == size))
    return zip(row[keep], col[keep])


def touched_squares(a, b, squares):
    """Closed segment/box intersection by interval clipping, not pixel sampling."""
    enter, leave = np.zeros(len(squares)), np.ones(len(squares))
    for axis in (0, 1):
        delta = b[axis]-a[axis]
        low, high = squares[:, axis]-.5, squares[:, axis]+.5
        if abs(delta) < 1e-12:
            enter[(a[axis] < low) | (a[axis] > high)] = np.inf
        else:
            first, last = (low-a[axis])/delta, (high-a[axis])/delta
            enter = np.maximum(enter, np.minimum(first, last))
            leave = np.minimum(leave, np.maximum(first, last))
    return enter <= leave+1e-10


@pytest.mark.parametrize('terrain', [1., 1.5])
def test_open_field_reaches_the_straight_line_distance_without_duplicate_edges(terrain):
    graph = build_graph(np.full((97, 97), terrain, np.float32), registration(97, 97), 'open-field')
    a, b = graph.snap([1.5, 1.5]), graph.snap([81.5, 41.5])
    distances, parents = graph.shortest_paths([a], targets=[b])
    direct = np.linalg.norm(graph.local[a]-graph.local[b])
    assert distances[0, b] == pytest.approx(direct*terrain, abs=1e-9)
    path = graph.expand(parents[0], a, b)
    assert sum(float(graph.lengths[u, v]) for u, v in zip(path, path[1:])) == pytest.approx(direct)
    row, col = graph.lengths.nonzero()
    expected = np.linalg.norm(graph.local[row]-graph.local[col], axis=1)
    np.testing.assert_allclose(np.asarray(graph.lengths[row, col]).ravel(), expected, rtol=0, atol=1e-10)
    assert len(list(new_edges(graph))) > 0


def test_slow_ground_between_equal_cost_endpoints_is_not_shortcut():
    costs = np.ones((17, 17), np.float32)
    costs[:, 6] = 3
    graph = build_graph(costs, registration(17, 17), 'slow-stripe')
    a, b = graph.snap([1.5, 1.5]), graph.snap([9.5, 5.5])
    assert graph.cell_sizes[a] == graph.cell_sizes[b] == 4
    assert graph.weights[a, b] == 0
    assert graph.components[a] == graph.components[b]


@pytest.mark.parametrize('seed', [18, 37, 62])
def test_shortcuts_do_not_touch_blocked_or_different_cost_pixel_squares(seed):
    rng = np.random.default_rng(seed)
    costs = np.ones((31, 43), np.float32)
    costs[rng.random(costs.shape) < .06] = 0
    costs[10:19, 8:14] = 1.5
    costs[3:7, 25:33] = 1.7
    graph = build_graph(costs, registration(43, 31), 'mixed-'+str(seed))
    yy, xx = np.indices(costs.shape)
    squares = np.column_stack((xx.ravel(), yy.ravel()))
    checked = 0
    for a, b in new_edges(graph):
        touched = touched_squares(graph.pixels[a], graph.pixels[b], squares)
        values = costs.ravel()[touched]
        assert len(values) and np.all(values > 0)
        assert np.all(values == values[0])
        assert np.all(graph.owner.ravel()[touched] >= 0)
        assert float(graph.weights[a, b]) == pytest.approx(float(graph.lengths[a, b])*values[0])
        checked += 1
    assert checked > 0
    # A corner contact cannot merge native four-connected components.
    regions, count = label(graph.owner >= 0)
    assert graph.component_count == count
    at = np.floor(graph.pixels+.5).astype(int)
    native = regions[at[:, 1], at[:, 0]]
    for region in range(1, count+1):
        assert len(np.unique(graph.components[native == region])) == 1


def test_one_pixel_gate_remains_usable_and_a_closed_gate_remains_disconnected():
    costs = np.ones((33, 33), np.float32)
    costs[:, 16] = 0
    costs[17, 16] = 1
    reg = registration(33, 33)
    graph = build_graph(costs, reg, 'one-pixel-gate')
    a, b = graph.snap([5, 25]), graph.snap([27, 25])
    distance, parents = graph.shortest_paths([a], targets=[b])
    assert np.isfinite(distance[0, b])
    blocked = np.column_stack(np.nonzero(costs == 0)[::-1])
    route = graph.expand(parents[0], a, b)
    assert any(abs(graph.pixels[n, 0]-16) <= 1 and abs(graph.pixels[n, 1]-17) <= 1 for n in route)
    for u, v in zip(route, route[1:]):
        assert not touched_squares(graph.pixels[u], graph.pixels[v], blocked).any()
    costs[17, 16] = 0
    closed = build_graph(costs, reg, 'closed-gate')
    a, b = closed.snap([5, 25]), closed.snap([27, 25])
    assert closed.components[a] != closed.components[b]


def test_warped_registration_keeps_new_edges_inside_one_affine_piece():
    source = np.array([[0, 0], [32, 0], [32, 32], [0, 32], [16, 16]], float)
    affine = [[1, 0, 0], [0, -1, 32]]
    target = affine_points(affine, source)
    target[-1] += [2, 1]
    reg = Registration(affine, source, target, regularization=0)
    graph = build_graph(np.ones((33, 33), np.float32), reg, 'warped')
    pieces = reg.tri.find_simplex(graph.pixels)
    checked = 0
    for a, b in new_edges(graph):
        assert pieces[a] == pieces[b]
        midpoint, valid = reg.forward([(graph.pixels[a]+graph.pixels[b])/2])
        assert valid[0]
        np.testing.assert_allclose(midpoint[0], (graph.local[a]+graph.local[b])/2, rtol=0, atol=1e-9)
        checked += 1
    assert checked > 0
    a, b = graph.snap([13.5, 9.5]), graph.snap([21.5, 13.5])
    assert pieces[a] != pieces[b] and graph.weights[a, b] == 0


def test_shared_sort_builds_the_same_csr_as_coo_conversion_and_rejects_repeats():
    from scipy.sparse import coo_matrix
    from dirmap.graph import _symmetric_csr
    rng = np.random.default_rng(7)
    count = 500
    keys = np.unique(rng.integers(0, count, (3000, 2)) @ np.array([count, 1]))
    a, b = (keys//count).astype(np.int32), (keys % count).astype(np.int32)
    a, b = a[a < b], b[a < b]
    weights, lengths = rng.random(len(a)), rng.random(len(a))
    weighted, distances = _symmetric_csr(count, a, b, weights, lengths)
    row, col = np.concatenate((a, b)), np.concatenate((b, a))
    for built, values in ((weighted, weights), (distances, lengths)):
        expected = coo_matrix((np.concatenate((values, values)), (row, col)), shape=(count, count)).tocsr()
        for key in ('indices', 'indptr', 'data'):
            assert np.array_equal(getattr(built, key), getattr(expected, key))
            assert getattr(built, key).dtype == getattr(expected, key).dtype
    with pytest.raises(ValueError, match='重复'):
        _symmetric_csr(count, np.r_[a, a[:1]], np.r_[b, b[:1]], np.r_[weights, 1.], np.r_[lengths, 1.])
