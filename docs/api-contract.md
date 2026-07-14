# API Contract — product repo ↔ kebi

> **This file is a pointer, not a copy.** The canonical API contract lives in
> the **product repo (`kebi-app`)** at `docs/api-contract.md`. Read and edit it
> there — do not re-add the full contract text here.

Maintaining a second full copy in this repo led to drift (the two versions fell
tens of KB apart), so the copy was collapsed to this pointer. There is now one
source of truth.

## Where it is

- **Canonical:** `kebi-app` repo → `docs/api-contract.md`
- Locally, `kebi-app` is a sibling of this repo, so the file is at
  `../kebi-app/docs/api-contract.md` relative to the repo root.

## Why the product repo is canonical

NestJS is the client that owns and publishes the contract; this repo (kebi) is
the server that implements it. Keeping the authoritative copy next to the client
matches how the contract is versioned and reviewed.

## When you change the contract

1. Edit `docs/api-contract.md` in the **product repo**.
2. Update this repo's server code, `docs/architecture.md`, and `docs/decisions.md`
   (a new ADR) to match — those stay in this repo.
3. Ship both repos in one coordinated deploy when the change is breaking (see the
   gateway-auth and table-rename coordination notes in the canonical contract).
