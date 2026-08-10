from menhir.domain.diversity import (
    dominance, normalized_entropy, is_collapsed, diversify,
)

def test_dominance_and_entropy():
    fams = ["a", "a", "a", "a"]
    assert dominance(fams) == 1.0
    assert normalized_entropy(fams) == 0.0
    mixed = ["a", "b", "c", "d"]
    assert dominance(mixed) == 0.25
    assert normalized_entropy(mixed) == 1.0  # uniform over 4 -> max entropy

def test_is_collapsed_triggers_on_dominance_or_low_entropy():
    assert is_collapsed(["a","a","a","b"], dominance_max=0.6, entropy_min=0.5) is True
    assert is_collapsed(["a","b","c","d"], dominance_max=0.6, entropy_min=0.5) is False

def test_diversify_noop_when_not_collapsed():
    items = [("m1","a"), ("m2","b"), ("m3","c")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=2,
                    dominance_max=0.6, entropy_min=0.5)
    assert out == items  # unchanged, stable

def test_diversify_interleaves_deterministically_when_collapsed():
    # 4 of family a, 1 of b: collapsed -> round-robin by family, within-family order kept,
    # families ordered by first appearance. Deterministic.
    items = [("a1","a"),("a2","a"),("a3","a"),("a4","a"),("b1","b")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=99,
                    dominance_max=0.6, entropy_min=0.9)
    assert out == [("a1","a"),("b1","b"),("a2","a"),("a3","a"),("a4","a")]

def test_diversify_is_pure_stable_permutation():
    items = [("a1","a"),("a2","a"),("b1","b"),("c1","c")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=99,
                    dominance_max=0.0, entropy_min=1.1)  # force collapsed
    assert sorted(out) == sorted(items)  # same multiset, reordered only

def test_diversify_clamps_nonpositive_max_per_family():
    items = [("a1","a"),("a2","a"),("b1","b")]
    out = diversify(items, family_of=lambda t: t[1], max_per_family=0,
                    dominance_max=0.0, entropy_min=1.1)  # force collapsed
    assert sorted(out) == sorted(items)  # terminates + pure permutation
