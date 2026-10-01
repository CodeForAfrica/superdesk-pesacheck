#!/usr/bin/env python3
"""Convert a `dump_api.py` workflow dump into the tracked workflow JSON.

The workflow config — desks, stages, roles — lives as tracked JSON under
`server/data/` beside the content config (AGENTS.md §4), and this is its
converter: `dump_api.py` captures a running instance over REST, this turns the
capture into `desks.json`, `stages.json` and `roles.json`, and the git diff is the
review. It owns those three files; `content_config/convert.py` does not write
desks or stages.

The dump carries the source instance's ObjectIds, and those are not ours: a
reference instance has its own profile, template and desk ids. So every
reference is resolved *by name* and re-pointed at the tracked tree:

  * a desk or stage that already exists in the tracked tree (matched by desk
    name, and stage name within it) keeps its tracked `_id`, so items already
    sitting on it stay homed; a new one takes the source `_id`, so the next
    refresh from the same source is a no-op diff;
  * a stage renamed on the way (STAGE_ALIASES) inherits the tracked `_id` of the
    stage it replaces;
  * a role keeps its tracked `_id` by name, else takes the source's;
  * `default_content_profile` is resolved via the source profile's label to the
    tracked `content_types.json` entry (PROFILE_ALIASES first, then a
    case-insensitive label match), and `default_content_template` to the tracked
    create-template for that profile. An unresolvable profile is an error, not a
    silent null.

Decisions (docs/plans/workflow-config-as-tracked-json.md §5) are the constants
below, so they re-apply on every refresh instead of being hand-edits a refresh
would undo.

Stdlib only; runs on the host:

  ./convert_api.py --source DUMP_DIR --privileges OUR_DUMP/privileges.json [--dest data]

`--privileges` is the privilege catalogue of the core the tracked tree seeds
(dump it from our own instance): privileges it does not register are dropped
from every role, and ALL_PRIVILEGE_ROLES get the whole catalogue.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]

# Source desk name -> tracked desk name.
DESK_ALIASES = {"Static Pages (formerly team members)": "Static Pages"}

# Desks whose tracked desk fields are kept verbatim; only their stages (and the
# incoming/working pointers into them) come from the source. Static Pages is the
# editorial importer's desk: its fixed `_id` keeps published pages homed across
# reseeds (AGENTS.md §4), and the source's version of it was repurposed from a
# team-members desk (default profile Team member), which ours is not.
TRACKED_DESKS = {"Static Pages"}

# (desk, source stage name) -> tracked stage name whose `_id` it inherits. The
# importer publishes onto the desk's incoming stage, so the stage replacing our
# old single "Pages" stage as incoming takes its pinned `_id`, and pages already
# published onto it stay on a live stage. Inert once the rename has landed (the
# name then matches directly); kept so a refresh from an old tree still maps it.
STAGE_ALIASES = {("Static Pages", "Incoming Stage"): "Pages"}

# Fields of an already-tracked desk that stay ours. `source` is stamped on every
# item created on the desk, so a reference instance's value ("sandbox tester")
# must not leak in; its description of Newsdesk as a testing desk is not ours.
TRACKED_DESK_FIELDS = {"source", "description"}

# Source profile label -> tracked content_types `_id`, where labels differ.
PROFILE_ALIASES = {"full article": "article"}

# Roles granted every privilege the target core registers ("all available
# permissions"), including ones the source's older core did not have.
ALL_PRIVILEGE_ROLES = {"Managing editor"}

# Never written: per-instance membership, and API/bookkeeping fields.
DROP_FIELDS = {
    "members",
    "_etag",
    "_links",
    "_updated",
    "_created",
    "_current_version",
    "_type",
}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json():
    # Reuse the content-config pretty-printer so every tracked file shares one format.
    spec = importlib.util.spec_from_file_location(
        "cc_convert", SERVER_ROOT / "scripts" / "content_config" / "convert.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_json


def clean(doc):
    return {k: v for k, v in doc.items() if k not in DROP_FIELDS}


class Resolver:
    """Maps source ids to tracked ids through names."""

    def __init__(self, source, dest):
        self.src_profiles = {
            p["_id"]: p.get("label") for p in load(source / "content_types.json")
        }
        self.src_templates = {
            t["_id"]: t for t in load(source / "content_templates.json")
        }
        tracked_profiles = load(dest / "content_types.json")
        self.profiles_by_label = {
            (p.get("label") or "").lower(): p["_id"] for p in tracked_profiles
        }
        self.profile_ids = {p["_id"] for p in tracked_profiles}
        self.create_templates = {}
        for t in sorted(load(dest / "content_templates.json"), key=lambda t: t["_id"]):
            profile = (t.get("data") or {}).get("profile")
            if t.get("template_type") == "create" and profile:
                self.create_templates.setdefault(profile, t["_id"])

    def profile(self, src_id, where):
        if src_id is None:
            return None
        label = self.src_profiles.get(src_id)
        if label is None:
            if src_id in self.profile_ids:  # already a tracked id (source is ours)
                return src_id
            raise SystemExit(f"{where}: profile {src_id!r} is not in the source dump")
        key = label.lower()
        tracked = PROFILE_ALIASES.get(key) or self.profiles_by_label.get(key)
        if tracked is None:
            raise SystemExit(
                f"{where}: source profile {label!r} has no tracked equivalent "
                "(add it to PROFILE_ALIASES or to content_types.json)"
            )
        return tracked

    def template(self, src_id, where):
        if src_id is None:
            return None
        src = self.src_templates.get(src_id)
        if src is None:
            raise SystemExit(f"{where}: template {src_id!r} is not in the source dump")
        profile = self.profile(
            src.get("profile"), f"{where} template {src.get('template_name')!r}"
        )
        return self.create_templates.get(profile)


def convert(source, dest, catalogue):
    resolver = Resolver(source, dest)
    tracked_desks = {d["name"]: d for d in load(dest / "desks.json")}
    tracked_stages = load(dest / "stages.json")
    tracked_stage_ids = {
        (d["name"], s["name"]): s["_id"]
        for d in tracked_desks.values()
        for s in tracked_stages
        if s["desk"] == d["_id"]
    }
    roles_file = dest / "roles.json"
    tracked_roles = (
        {r["name"]: r for r in load(roles_file)} if roles_file.exists() else {}
    )

    src_desks = load(source / "desks.json")
    src_stages = load(source / "stages.json")
    desks, stages = [], []

    for src in src_desks:
        name = DESK_ALIASES.get(src["name"], src["name"])
        where = f"desk {name!r}"
        tracked = tracked_desks.get(name)
        if name in TRACKED_DESKS and tracked is None:
            raise SystemExit(
                f"{where} is pinned to its tracked definition but is not tracked"
            )

        desk_id = tracked["_id"] if tracked else src["_id"]
        stage_ids = {}
        own = sorted(
            (s for s in src_stages if s["desk"] == src["_id"]),
            key=lambda s: s.get("desk_order") or 0,
        )
        for order, s in enumerate(own, start=1):
            sid = (
                tracked_stage_ids.get((name, s["name"]))
                or tracked_stage_ids.get((name, STAGE_ALIASES.get((name, s["name"]))))
                or s["_id"]
            )
            stage_ids[s["_id"]] = sid
            # desk_order renumbered contiguous: a deleted stage leaves a gap.
            stages.append(
                {**clean(s), "_id": sid, "desk": desk_id, "desk_order": order}
            )

        for field in ("incoming_stage", "working_stage"):
            if src.get(field) not in stage_ids:
                raise SystemExit(
                    f"{where}: {field} {src.get(field)!r} is not one of its stages"
                )
        if name in TRACKED_DESKS:
            desks.append(
                {
                    **tracked,
                    "incoming_stage": stage_ids[src["incoming_stage"]],
                    "working_stage": stage_ids[src["working_stage"]],
                }
            )
            continue

        desk = {**clean(src), "_id": desk_id, "name": name}
        if tracked:
            for field in TRACKED_DESK_FIELDS:
                desk.pop(field, None)
                if field in tracked:
                    desk[field] = tracked[field]
        for field in ("incoming_stage", "working_stage"):
            desk[field] = stage_ids[src[field]]
        desk["default_content_profile"] = resolver.profile(
            src.get("default_content_profile"), where
        )
        desk["default_content_template"] = resolver.template(
            src.get("default_content_template"), where
        )
        desks.append(desk)

    missing = set(tracked_desks) - {d["name"] for d in desks}
    if missing:
        # A tracked desk vanishing is a review decision, never a refresh side effect.
        raise SystemExit(f"tracked desks absent from the source: {sorted(missing)}")

    roles, dropped = [], {}
    for src in load(source / "roles.json"):
        granted = {p for p, v in (src.get("privileges") or {}).items() if v}
        if src["name"] in ALL_PRIVILEGE_ROLES:
            granted = set(catalogue)
        unknown = granted - catalogue
        if unknown:
            dropped[src["name"]] = sorted(unknown)
        tracked = tracked_roles.get(src["name"])
        roles.append(
            {
                **clean(src),
                "_id": tracked["_id"] if tracked else src["_id"],
                "privileges": {p: 1 for p in sorted(granted & catalogue)},
            }
        )

    by_id = lambda d: d["_id"]  # noqa: E731
    return (
        sorted(desks, key=by_id),
        sorted(stages, key=by_id),
        sorted(roles, key=by_id),
        dropped,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", required=True, type=Path, help="dump_api.py output directory"
    )
    parser.add_argument(
        "--privileges",
        required=True,
        type=Path,
        help="privileges.json from a dump of the instance the tracked tree seeds",
    )
    parser.add_argument("--dest", default=SERVER_ROOT / "data", type=Path)
    args = parser.parse_args(argv)

    catalogue = {p["name"] for p in load(args.privileges)}
    desks, stages, roles, dropped = convert(args.source, args.dest, catalogue)
    write_json = _write_json()
    write_json(args.dest / "desks.json", desks)
    write_json(args.dest / "stages.json", stages)
    write_json(args.dest / "roles.json", roles)
    for role, privileges in dropped.items():
        print(
            f"!! {role}: dropped privileges the target core does not register: {privileges}",
            file=sys.stderr,
        )
    print(
        f">> wrote {len(desks)} desks, {len(stages)} stages, {len(roles)} roles to {args.dest}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
