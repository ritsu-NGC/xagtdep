"""
Priority-cut-style Reed-Muller (ANF) network builder with node sharing.

Extends `build_reed_muller_network`'s simple prefix-chain caching (which
only reuses a node if some OTHER monomial happens to share an exact
prefix, e.g. (0,1,2) and (0,1,2,3)) with genuine priority-cut-style
matching: for each monomial (a frozenset of variable indices), we search
ALL previously built subset-AND nodes for the largest one whose variable
set is a SUBSET of the monomial -- not just a prefix -- and extend that
cut with the remaining variables. Ties are broken by preferring cuts
that have already been reused the most (a simple stand-in for ABC's
priority-cut area/depth heuristics), which tends to concentrate reuse
onto a small set of "popular" shared subexpressions.

This requires `PebblingNetwork`, `reed_muller_decompose`, and
`build_gate_groups`/`pebble_gates`/etc. from `pebbling_solver.py`.
"""

from itertools import combinations
from pebbling_solver import (
    PebblingNetwork,
    reed_muller_decompose,
    random_boolean_bit_mapping,
)


def build_reed_muller_network_priority_cuts(num_vars, truth_tables, var_names=None):
    """
    Builds a `PebblingNetwork` from a list of Boolean truth tables via
    Reed-Muller (ANF) decomposition, using PRIORITY-CUT-STYLE node
    sharing: every monomial's AND-node is built by finding the LARGEST
    already-existing subset-AND node whose variable set is contained in
    the monomial (not merely a matching prefix), then extending it with
    the remaining variables one at a time. All intermediate subset
    nodes created along the way are cached by their exact frozenset of
    variable indices, so later monomials (from this OR a later output)
    can match against them too.

    Cut selection priority (highest first):
      1. size of the matched cut (more leaves covered => fewer AND
         gates needed to extend it)
      2. how many times that cut has already been reused (favors
         concentrating reuse onto a small set of "hot" subexpressions,
         mirroring how priority-cut mapping favors well-utilized cuts)

    Returns the constructed `PebblingNetwork`, with one PO per input
    truth table (in order).
    """
    net = PebblingNetwork()
    var_names = var_names or [f"x{i}" for i in range(num_vars)]
    pis = [net.create_pi(var_names[i]) for i in range(num_vars)]

    const_nodes = {}

    def get_const(name):
        if name not in const_nodes:
            const_nodes[name] = net.create_pi(name)
        return const_nodes[name]

    # cache: frozenset(var indices) -> Node
    subset_cache = {}
    # reuse_count: frozenset(var indices) -> int (times matched as a cut
    # for some OTHER, larger monomial -- used purely for priority
    # tie-breaking, not for correctness)
    reuse_count = {}

    and_counter = [0]

    def register(vars_fs, node):
        subset_cache[vars_fs] = node
        reuse_count.setdefault(vars_fs, 0)
        return node

    for i in range(num_vars):
        register(frozenset({i}), pis[i])

    def find_best_cut(monomial_fs):
        """
        Finds the best already-cached subset of `monomial_fs` to use as
        a starting cut, per the priority rules above. Returns
        (best_subset_fs, best_node) or (frozenset(), None) if nothing
        smaller than the full monomial is cached (i.e. must start from
        scratch / single variables).
        """
        best = None
        best_key = None
        for cached_fs in subset_cache:
            if cached_fs == monomial_fs:
                continue
            if cached_fs < monomial_fs:  # proper subset
                key = (len(cached_fs), reuse_count.get(cached_fs, 0))
                if best_key is None or key > best_key:
                    best_key = key
                    best = cached_fs
        if best is None:
            return frozenset(), None
        return best, subset_cache[best]

    def get_and_node(monomial_fs):
        if not monomial_fs:
            return get_const("const1")
        if monomial_fs in subset_cache:
            reuse_count[monomial_fs] = reuse_count.get(monomial_fs, 0) + 1
            return subset_cache[monomial_fs]

        cut_fs, cut_node = find_best_cut(monomial_fs)
        if cut_node is not None:
            reuse_count[cut_fs] = reuse_count.get(cut_fs, 0) + 1
        else:
            # fall back to a single variable as the seed
            seed_var = min(monomial_fs)
            cut_fs, cut_node = frozenset({seed_var}), pis[seed_var]

        remaining = sorted(monomial_fs - cut_fs)
        current_fs, current_node = cut_fs, cut_node
        for v in remaining:
            next_fs = current_fs | {v}
            if next_fs in subset_cache:
                current_fs, current_node = next_fs, subset_cache[next_fs]
                reuse_count[next_fs] = reuse_count.get(next_fs, 0) + 1
                continue
            and_counter[0] += 1
            term_str = "_".join(str(x) for x in sorted(next_fs))
            new_node = net.create_and_gate(
                [current_node, pis[v]], name=f"and{and_counter[0]}_{term_str}"
            )
            register(next_fs, new_node)
            current_fs, current_node = next_fs, new_node

        return current_node

    xor_cache = {}
    xor_counter = [0]

    def get_xor_of(node_a, node_b):
        key = frozenset((id(node_a), id(node_b)))
        if key in xor_cache:
            return xor_cache[key]
        xor_counter[0] += 1
        node = net.create_xor_gate([node_a, node_b], name=f"xor{xor_counter[0]}")
        xor_cache[key] = node
        return node

    for tt in truth_tables:
        monomials = reed_muller_decompose(tt, num_vars)
        if not monomials:
            po_node = get_const("const0")
        else:
            # Larger monomials first: gives bigger cuts a chance to be
            # cached BEFORE smaller monomials look for something to
            # match against, which tends to produce better sharing.
            monomials_sorted = sorted(monomials, key=lambda t: (-len(t), t))
            term_nodes = [get_and_node(frozenset(t)) for t in monomials_sorted]
            po_node = term_nodes[0]
            for nxt in term_nodes[1:]:
                po_node = get_xor_of(po_node, nxt)
        net.create_po(po_node)

    return net


def random_reed_muller_priority_cut_dag(num_vars, num_outputs, seed=None,
                                          density=0.5, var_names=None):
    """
    Convenience wrapper: random n-PI, m-PO Boolean bit mapping -> ANF
    decomposition -> priority-cut-shared PebblingNetwork.
    """
    truth_tables = random_boolean_bit_mapping(
        num_vars, num_outputs=num_outputs, seed=seed, density=density
    )
    return build_reed_muller_network_priority_cuts(
        num_vars, truth_tables, var_names=var_names
    )
