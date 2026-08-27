import numpy as np

from guvcfd.cellzones import bin_decay_rates


def test_bin_representative_value_uses_actual_occupant_cells_not_bin_edges():
    # Regression guard for a real, confirmed gap: a sparse, tightly-clustered
    # top bin (e.g. a handful of cells right at a locally-refined near-field
    # peak) got the bin's theoretical edge-geometric-mean as its
    # representative value, understating what its actual cells needed by
    # ~17% on a real case. The representative value must instead reflect
    # the bin's own real occupants whenever it has any.
    k_values = np.array([0.0, 0.0, 1.0, 1.0, 103.736, 105.458, 106.979, 109.807])
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins=25)

    top_bin = bin_idx[-1]  # the highest cell (109.807) is in the top occupied bin
    top_cells = k_values[bin_idx == top_bin]
    assert set(top_cells) == {103.736, 105.458, 106.979, 109.807}

    expected = np.exp(np.mean(np.log(top_cells)))
    assert bin_repr[top_bin] == expected
    # The old edge-based value (88.6-ish for this exact data) must NOT be
    # what gets used - the fix moves it meaningfully closer to the cells'
    # own actual magnitude.
    assert abs(bin_repr[top_bin] - expected) < 1e-9
    assert bin_repr[top_bin] > 100  # not the understated edge-based ~88.6


def test_empty_bin_falls_back_to_edge_based_geometric_mean():
    # Sparse/clustered values can leave some log-spaced bins with zero real
    # cells - nothing to average, so the edge-based geometric mean is the
    # only sensible fallback there.
    k_values = np.array([1.0, 1000.0])  # 3 orders of magnitude apart, few bins
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins=10)

    occupied = set(bin_idx[k_values > 0])
    empty_bins = [b for b in range(1, 11) if b not in occupied]
    assert empty_bins  # this data must actually produce at least one empty bin
    edges = np.logspace(0, 3, 11)
    for b in empty_bins:
        lo, hi = edges[b - 1], edges[b]
        assert bin_repr[b] == np.sqrt(lo * hi)


def test_zero_values_always_land_in_bin_zero_with_zero_representative():
    k_values = np.array([0.0, 0.0, 5.0, 10.0])
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins=5)
    assert bin_idx[0] == 0 and bin_idx[1] == 0
    assert bin_repr[0] == 0.0


def test_all_zero_raises():
    import pytest
    with pytest.raises(RuntimeError, match="All decay rates are zero"):
        bin_decay_rates(np.array([0.0, 0.0]), nbins=5)


def test_bin_count_matches_nbins_plus_zero_bin():
    k_values = np.array([1.0, 2.0, 5.0, 10.0, 50.0])
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins=25)
    assert len(bin_repr) == 26  # nbins + 1 (zero bin)
    assert bin_idx.max() <= 25
