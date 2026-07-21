"""Stage 8: canonical-record selection (Section 8). The sole rule in
this release: the lowest row_index in the group -- a fixed, deterministic,
content-independent tie-break, not organization-configurable."""


def select_canonical(member_row_indices: list[int]) -> int:
    return min(member_row_indices)
