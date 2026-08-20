"""Geo identity registry — stable provider ids for areas, names as data.

The registry is the single source of geo identity: one row per geographic
unit ever seen, keyed by the provider's stable place id, minted lazily the
first time a save or claim names an area the registry doesn't know. Keys
are built from ids; every name a user sees is registry data. The
hand-maintained fold tables this replaces lived in
`core.knowledge.schemas` — see docs/plans/2026-08-20-geo-identity-registry.md.
"""
