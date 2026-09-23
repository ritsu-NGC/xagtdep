import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pebbling_solver import GatePebbleSolver, build_gate_groups, _pebble_cost
from pebbling_solver import PebblingNetwork


def _build_grouping_network(extra_xors=0, extra_ands=0):
    net = PebblingNetwork()
    a = net.create_pi("a")
    b = net.create_pi("b")
    c = net.create_pi("c")
    d = net.create_pi("d")
    e = net.create_pi("e")
    f = net.create_pi("f")

    cur = net.create_and_gate([a, b], name="and0")
    cur = net.create_xor_gate([cur, c], name="xor0")
    for i in range(extra_xors):
        fanin = d if i % 2 == 0 else e
        cur = net.create_xor_gate([cur, fanin], name=f"xor_extra_{i}")

    cur = net.create_and_gate([cur, d], name="and1")
    for i in range(extra_ands):
        fanin = e if i % 2 == 0 else f
        cur = net.create_and_gate([cur, fanin], name=f"and_extra_{i}")

    net.create_po(cur)
    return net


class PebblingSolverXorCostTest(unittest.TestCase):
    def test_pebble_cost_excludes_xor_nodes(self):
        net = PebblingNetwork()
        a = net.create_pi("a")
        b = net.create_pi("b")
        xor_node = net.create_xor_gate([a, b], name="xor")
        and_node = net.create_and_gate([a, b], name="and")

        self.assertEqual(_pebble_cost(xor_node), 0)
        self.assertEqual(_pebble_cost(and_node), 1)

    def test_xor_nodes_do_not_change_group_boundaries_but_ands_do(self):
        base_groups = build_gate_groups(_build_grouping_network(), max_pebbles=3)
        xor_heavy_groups = build_gate_groups(
            _build_grouping_network(extra_xors=4), max_pebbles=3
        )
        and_heavy_groups = build_gate_groups(
            _build_grouping_network(extra_ands=1), max_pebbles=3
        )

        self.assertEqual(len(base_groups), 1)
        self.assertEqual(len(xor_heavy_groups), 1)
        self.assertEqual(len(and_heavy_groups), 2)

        base_and_partition = [
            [node.name for node in group.nodes if not node.is_xor]
            for group in base_groups
        ]
        xor_heavy_and_partition = [
            [node.name for node in group.nodes if not node.is_xor]
            for group in xor_heavy_groups
        ]
        self.assertEqual(base_and_partition, xor_heavy_and_partition)

    def test_gate_solver_only_exempts_xor_only_groups(self):
        net = PebblingNetwork()
        a = net.create_pi("a")
        b = net.create_pi("b")
        c = net.create_pi("c")
        d = net.create_pi("d")
        e = net.create_pi("e")

        and0 = net.create_and_gate([a, b], name="and0")
        xor0 = net.create_xor_gate([and0, c], name="xor0")
        and1 = net.create_and_gate([xor0, d], name="and1")
        xor1 = net.create_xor_gate([and1, e], name="xor1")
        net.create_po(xor1)

        groups = build_gate_groups(net, max_pebbles=3)
        self.assertEqual(
            [[n.name for n in g.nodes] for g in groups],
            [["and0", "xor0", "and1"], ["xor1"]],
        )

        solver = GatePebbleSolver(groups, max_pebbles=1, net=net)
        self.assertTrue(solver.counts_toward_limit[groups[0].gid])
        self.assertFalse(solver.counts_toward_limit[groups[1].gid])


if __name__ == "__main__":
    unittest.main()
