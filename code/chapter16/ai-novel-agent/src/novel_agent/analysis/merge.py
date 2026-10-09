"""Pure, conservative aggregation for validated full-analysis batch candidates."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BatchCandidate:
    index: int
    candidate: dict[str, Any]


@dataclass(frozen=True)
class _Mention:
    group: int
    character: dict[str, Any]


@dataclass
class _PrimaryGroup:
    primary: str
    first_order: int


@dataclass
class _OutputCluster:
    character: dict[str, Any]
    alias_keys: set[str]
    evidence_keys: set[str]
    description_keys: set[str]
    trait_keys: set[str]


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(left_root, right_root)


def normalize_identity(value: str) -> str:
    """Normalize only the forms explicitly allowed for deterministic identity matching."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _extend_unique(target: list[Any], seen: set[str], values: Iterable[Any]) -> None:
    for value in values:
        key = _canonical(value)
        if key not in seen:
            target.append(deepcopy(value))
            seen.add(key)


def _identity_values(character: dict[str, Any]) -> list[str]:
    return [character["name"], *character.get("aliases", [])]


def _new_output_cluster(character: dict[str, Any]) -> _OutputCluster:
    copied = deepcopy(character)
    primary = normalize_identity(character["name"])
    aliases: list[str] = []
    alias_keys = {primary}
    for value in character.get("aliases", []):
        normalized = normalize_identity(value)
        if normalized and normalized not in alias_keys:
            aliases.append(deepcopy(value))
            alias_keys.add(normalized)
    copied["aliases"] = aliases
    return _OutputCluster(
        character=copied,
        alias_keys=alias_keys,
        evidence_keys={_canonical(value) for value in copied.get("evidence", [])},
        description_keys={_canonical(value) for value in copied.get("description", [])},
        trait_keys={_canonical(value) for value in copied.get("traits", [])},
    )


def _merge_character(cluster: _OutputCluster, character: dict[str, Any]) -> None:
    for value in _identity_values(character):
        normalized = normalize_identity(value)
        if normalized and normalized not in cluster.alias_keys:
            cluster.character["aliases"].append(deepcopy(value))
            cluster.alias_keys.add(normalized)
    _extend_unique(
        cluster.character["evidence"],
        cluster.evidence_keys,
        character.get("evidence", []),
    )
    _extend_unique(
        cluster.character["description"],
        cluster.description_keys,
        character.get("description", []),
    )
    _extend_unique(
        cluster.character["traits"],
        cluster.trait_keys,
        character.get("traits", []),
    )


def _connected_components(neighbors: dict[int, set[int]]) -> list[set[int]]:
    remaining = set(neighbors)
    components: list[set[int]] = []
    while remaining:
        start = min(remaining)
        pending = [start]
        component: set[int] = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current] - component)
        remaining.difference_update(component)
        components.append(component)
    return components


def merge_batches(items: Sequence[BatchCandidate]) -> dict[str, Any]:
    """Merge validated candidates using a global, order-independent identity index."""
    summary: list[Any] = []
    summary_keys: set[str] = set()
    mentions: list[_Mention] = []
    groups: list[_PrimaryGroup] = []
    primary_groups: dict[str, int] = {}
    token_groups: dict[str, set[int]] = {}
    token_display: dict[str, str] = {}
    token_order: dict[str, int] = {}

    for item in sorted(items, key=lambda candidate: candidate.index):
        _extend_unique(summary, summary_keys, item.candidate.get("summary", []))
        for character in item.candidate.get("characters", []):
            primary = normalize_identity(character["name"])
            group_id = primary_groups.get(primary)
            if group_id is None:
                group_id = len(groups)
                primary_groups[primary] = group_id
                groups.append(_PrimaryGroup(primary=primary, first_order=len(mentions)))
            mentions.append(_Mention(group_id, character))
            for value in _identity_values(character):
                token = normalize_identity(value)
                if not token:
                    continue
                token_groups.setdefault(token, set()).add(group_id)
                token_display.setdefault(token, value)
                token_order.setdefault(token, len(token_order))

    candidate_edges: dict[frozenset[int], set[str]] = {}
    conflict_edges: dict[frozenset[int], set[str]] = {}
    hyper_edges: dict[frozenset[int], set[str]] = {}
    for token, token_related_groups in token_groups.items():
        if len(token_related_groups) == 2:
            pair = frozenset(token_related_groups)
            if any(groups[group_id].primary == token for group_id in token_related_groups):
                candidate_edges.setdefault(pair, set()).add(token)
            else:
                conflict_edges.setdefault(pair, set()).add(token)
        elif len(token_related_groups) > 2:
            hyper_edges.setdefault(frozenset(token_related_groups), set()).add(token)

    neighbors: dict[int, set[int]] = {}
    for pair in [*candidate_edges, *conflict_edges]:
        left, right = sorted(pair)
        neighbors.setdefault(left, set()).add(right)
        neighbors.setdefault(right, set()).add(left)
    for hyper_related_groups in hyper_edges:
        ordered = sorted(hyper_related_groups)
        anchor = ordered[0]
        for group_id in ordered[1:]:
            neighbors.setdefault(anchor, set()).add(group_id)
            neighbors.setdefault(group_id, set()).add(anchor)

    accepted_pairs: set[frozenset[int]] = set()
    components = _connected_components(neighbors)
    group_component = {
        group_id: component_index
        for component_index, component in enumerate(components)
        for group_id in component
    }
    component_tokens = [set[str]() for _ in components]
    for relations in (candidate_edges, conflict_edges, hyper_edges):
        for relation_groups, relation_tokens in relations.items():
            component_tokens[group_component[next(iter(relation_groups))]].update(
                relation_tokens
            )

    issue_specs: list[tuple[int, str, frozenset[int], set[str]]] = []
    for component_index, component in enumerate(components):
        component_groups = frozenset(component)
        if len(component) == 2 and component_groups in candidate_edges:
            accepted_pairs.add(component_groups)
            continue
        reason = "conflicting_primary_names" if len(component) == 2 else "bridge_identity"
        issue_specs.append(
            (
                min(groups[group_id].first_order for group_id in component_groups),
                reason,
                component_groups,
                component_tokens[component_index],
            )
        )

    disjoint = _DisjointSet(len(groups))
    for pair in accepted_pairs:
        left, right = sorted(pair)
        disjoint.union(left, right)

    output_clusters: list[_OutputCluster] = []
    root_to_output: dict[int, int] = {}
    for mention in mentions:
        root = disjoint.find(mention.group)
        output_index = root_to_output.get(root)
        if output_index is None:
            root_to_output[root] = len(output_clusters)
            output_clusters.append(_new_output_cluster(mention.character))
        else:
            _merge_character(output_clusters[output_index], mention.character)

    group_to_output = {
        group_id: root_to_output[disjoint.find(group_id)] for group_id in range(len(groups))
    }

    issue_specs.sort(key=lambda issue: (issue[0], min(token_order[token] for token in issue[3])))

    suggestions: list[dict[str, Any]] = []
    for _, reason, issue_groups, issue_tokens in issue_specs:
        indexes = sorted({group_to_output[group_id] for group_id in issue_groups})
        evidence: list[Any] = []
        evidence_keys: set[str] = set()
        for mention in mentions:
            if mention.group in issue_groups:
                _extend_unique(
                    evidence,
                    evidence_keys,
                    mention.character.get("evidence", []),
                )
        ordered_tokens = sorted(issue_tokens, key=token_order.__getitem__)
        suggestions.append(
            {
                "character_indexes": indexes,
                "reason": reason,
                "shared_identities": [token_display[token] for token in ordered_tokens],
                "evidence": evidence,
            }
        )

    return {
        "summary": summary,
        "characters": [deepcopy(cluster.character) for cluster in output_clusters],
        "merge_suggestions": suggestions,
    }
