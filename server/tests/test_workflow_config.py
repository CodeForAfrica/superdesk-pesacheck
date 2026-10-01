"""Conformance of the tracked workflow config (desks, stages, roles).

The workflow is drop-loaded from `server/data/` on every bootstrap
(`pesacheck/content_config_patch.py`), bypassing the desk/stage service hooks
that would otherwise keep references consistent. So nothing at load time stops a
desk pointing at another desk's stage, or at a template that does not exist.
These checks are that guard. They read the tracked tree only: no database, no
app context.
"""

import ast
import json
import re
import unittest
from collections import Counter, defaultdict

from tests.content_config import DATA_ROOT, SERVER_ROOT

# Resolved by name elsewhere and pinned by id so content stays homed across
# reseeds (AGENTS.md §4): the editorial importer's desk and the original intake desk.
PINNED_DESK_IDS = {
    "Newsdesk": "6a60cec4359af00f83894212",
    "Static Pages": "6aa5b1a5000000000000de5c",
}

# The editorial importer publishes onto Static Pages' incoming stage, so that
# stage keeps the id already-published pages carry in `task.stage`.
PINNED_INCOMING_STAGE_IDS = {"Static Pages": "6aa5b1a5000000000000de5d"}

ALL_PRIVILEGE_ROLE = "Managing editor"
OBJECT_ID = re.compile(r"^[0-9a-f]{24}$")


def load(name):
    return json.loads((DATA_ROOT / name).read_text(encoding="utf-8"))


def default_publish_desk():
    """DEFAULT_PUBLISH_DESK, read from source so the test needs no superdesk import."""
    source = (
        SERVER_ROOT / "pesacheck" / "ingest" / "ghost_feeding_service.py"
    ).read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "DEFAULT_PUBLISH_DESK" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULT_PUBLISH_DESK not found in ghost_feeding_service.py")


class WorkflowConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desks = load("desks.json")
        cls.stages = load("stages.json")
        cls.roles = load("roles.json")
        cls.profiles = {p["_id"]: p for p in load("content_types.json")}
        cls.templates = {t["_id"]: t for t in load("content_templates.json")}
        cls.stages_by_id = {s["_id"]: s for s in cls.stages}
        cls.stages_by_desk = defaultdict(list)
        for s in cls.stages:
            cls.stages_by_desk[s["desk"]].append(s)

    def test_ids_are_plain_object_ids(self):
        # core's _mongotize turns 24-hex into ObjectId; Extended JSON breaks it.
        for doc in self.desks + self.stages + self.roles:
            self.assertRegex(doc["_id"], OBJECT_ID, doc.get("name"))

    def test_names_are_unique(self):
        for kind, names in (
            ("desk", [d["name"] for d in self.desks]),
            ("role", [r["name"] for r in self.roles]),
        ):
            dupes = [n for n, c in Counter(names).items() if c > 1]
            self.assertFalse(dupes, f"duplicate {kind} names: {dupes}")
        for desk_id, stages in self.stages_by_desk.items():
            dupes = [n for n, c in Counter(s["name"] for s in stages).items() if c > 1]
            self.assertFalse(dupes, f"desk {desk_id}: duplicate stage names {dupes}")

    def test_every_stage_belongs_to_a_desk(self):
        desk_ids = {d["_id"] for d in self.desks}
        orphans = [s["name"] for s in self.stages if s["desk"] not in desk_ids]
        self.assertFalse(orphans, f"stages on no tracked desk: {orphans}")

    def test_desk_stage_references(self):
        for desk in self.desks:
            for field, flag in (
                ("incoming_stage", "default_incoming"),
                ("working_stage", "working_stage"),
            ):
                stage = self.stages_by_id.get(desk.get(field))
                self.assertIsNotNone(
                    stage, f"{desk['name']}: {field} is not a tracked stage"
                )
                self.assertEqual(
                    stage["desk"],
                    desk["_id"],
                    f"{desk['name']}: {field} is another desk's stage",
                )
                self.assertTrue(
                    stage.get(flag),
                    f"{desk['name']}: {field} {stage['name']!r} lacks {flag}",
                )
            for flag in ("default_incoming", "working_stage"):
                flagged = [
                    s["name"] for s in self.stages_by_desk[desk["_id"]] if s.get(flag)
                ]
                self.assertEqual(
                    len(flagged),
                    1,
                    f"{desk['name']}: expected one {flag} stage, got {flagged}",
                )

    def test_desk_order_is_contiguous(self):
        for desk in self.desks:
            orders = sorted(
                s.get("desk_order") for s in self.stages_by_desk[desk["_id"]]
            )
            self.assertEqual(orders, list(range(1, len(orders) + 1)), desk["name"])

    def test_desk_defaults_resolve(self):
        for desk in self.desks:
            profile = desk.get("default_content_profile")
            self.assertIn(
                profile, self.profiles, f"{desk['name']}: unknown default profile"
            )
            template_id = desk.get("default_content_template")
            if template_id is None:
                continue
            template = self.templates.get(template_id)
            self.assertIsNotNone(template, f"{desk['name']}: unknown default template")
            self.assertEqual(template.get("template_type"), "create", desk["name"])
            self.assertEqual(
                (template.get("data") or {}).get("profile"), profile, desk["name"]
            )
            # The Create menu offers a desk-scoped template only on its desks.
            scoped = template.get("template_desks") or []
            self.assertTrue(
                not scoped or desk["_id"] in scoped,
                f"{desk['name']}: default template {template['template_name']!r} is scoped to other desks",
            )

    def test_pinned_desk_ids(self):
        by_name = {d["name"]: d["_id"] for d in self.desks}
        for name, desk_id in PINNED_DESK_IDS.items():
            self.assertEqual(
                by_name.get(name), desk_id, f"{name} must keep its tracked _id"
            )
        incoming = {d["name"]: d["incoming_stage"] for d in self.desks}
        for name, stage_id in PINNED_INCOMING_STAGE_IDS.items():
            self.assertEqual(
                incoming.get(name),
                stage_id,
                f"{name}'s incoming stage must keep its _id",
            )

    def test_ghost_publish_desk_is_tracked(self):
        desk_name = default_publish_desk()
        self.assertIn(desk_name, {d["name"] for d in self.desks})
        script = (
            SERVER_ROOT / "docker" / "bootstrap" / "bootstrap-ghost-ingest.sh"
        ).read_text()
        self.assertIn(
            f'PUBLISH_DESK="${{PUBLISH_DESK:-{desk_name}}}"',
            script,
            "bootstrap-ghost-ingest.sh's PUBLISH_DESK default must match DEFAULT_PUBLISH_DESK",
        )

    def test_all_privilege_role_holds_every_granted_privilege(self):
        # The loader checks against core's live registry at seed time; offline,
        # the strongest check is that no role grants what the "all" role lacks.
        roles = {r["name"]: r for r in self.roles}
        self.assertIn(ALL_PRIVILEGE_ROLE, roles)
        everything = set(roles[ALL_PRIVILEGE_ROLE]["privileges"])
        for role in self.roles:
            extra = set(role.get("privileges") or {}) - everything
            self.assertFalse(
                extra,
                f"{role['name']} grants {sorted(extra)} that {ALL_PRIVILEGE_ROLE} lacks",
            )
