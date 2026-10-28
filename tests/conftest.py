"""Unit/API tests must not evict graphs prepared for the user's courses."""
import pytest


@pytest.fixture(autouse=True)
def isolated_scratch_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('DIRMAP_CACHE_DIR', str(tmp_path/'scratch'))


@pytest.fixture(autouse=True)
def isolated_public_samples(tmp_path, monkeypatch):
    # Unit/API fixtures provide their own miniature sample bundle when
    # needed; locally built production samples must not affect these tests.
    from dirmap import samples
    monkeypatch.setattr(samples, 'BUNDLE', tmp_path/'public-samples.json')
