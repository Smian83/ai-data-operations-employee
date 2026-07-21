"""Deterministic union-find (disjoint-set) clustering (Section 6, Stage
3). The resulting partition into connected components depends only on
which pairs were unioned, never on the order unions were applied in --
so this is deterministic regardless of dict/iteration order upstream."""


class UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1

    def components(self) -> dict[int, list[int]]:
        """Every element grouped by its final root, in row-index order
        within each group. Component membership is a pure function of
        the edges unioned -- independent of union call order."""
        members_by_root: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            root = self.find(i)
            members_by_root.setdefault(root, []).append(i)
        return members_by_root
