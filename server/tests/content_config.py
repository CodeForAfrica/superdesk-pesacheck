"""Read the vocabularies and content profiles out of the tracked content-config dump.

`docker/bootstrap/superdesk-content-config.tgz` is a periodically re-exported
UAT mongodump, and `bootstrap_superdesk.py` (`restore_content_config`) drops and
replaces the whole `vocabularies` and `content_types` collections from it. So it
-- not `server/data/vocabularies.json` -- is what actually defines the qcodes a
deployed newsroom recognises.

That makes it the thing every derived-field classifier in `pesacheck/` has to be
validated against, which is what this module exists for: it is tracked, it is
21 KB, and reading it needs no database and no app context. Any override
`bootstrap_superdesk.py` applies on top of the dump is applied here too, so what
these helpers return is what a seeded newsroom actually holds -- see
`applied_vocabularies`.
"""

import importlib.util
import tarfile
import tempfile
from functools import lru_cache
from pathlib import Path

from bson import decode_file_iter

SERVER_ROOT = Path(__file__).resolve().parent.parent
CONTENT_CONFIG_ARCHIVE = (
    SERVER_ROOT / "docker" / "bootstrap" / "superdesk-content-config.tgz"
)
BOOTSTRAP_SCRIPT = SERVER_ROOT / "docker" / "bootstrap" / "bootstrap_superdesk.py"


@lru_cache(maxsize=1)
def bootstrap():
    """Import `bootstrap_superdesk.py` by path.

    It lives under `docker/` rather than in an importable package, because it is
    an entrypoint script rather than library code. Loading it here keeps the
    override constants in exactly one place instead of restating them in tests.
    """
    spec = importlib.util.spec_from_file_location(
        "bootstrap_superdesk", BOOTSTRAP_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(collection):
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(CONTENT_CONFIG_ARCHIVE) as archive:
            archive.extractall(tmpdir, filter="data")
        paths = list(Path(tmpdir).rglob(f"{collection}.bson"))
        if not paths:
            raise AssertionError(
                f"No {collection}.bson in {CONTENT_CONFIG_ARCHIVE}; the dump changed shape."
            )
        with paths[0].open("rb") as handle:
            return list(decode_file_iter(handle))


@lru_cache(maxsize=1)
def dumped_vocabularies():
    """{vocabulary _id: document}, exactly as the dump defines it."""
    return {doc["_id"]: doc for doc in _load("vocabularies")}


@lru_cache(maxsize=1)
def content_profiles():
    """{profile label: document}, exactly as the dump defines it."""
    return {doc.get("label"): doc for doc in _load("content_types")}


@lru_cache(maxsize=1)
def applied_vocabularies():
    """The dump's vocabularies with the bootstrap overrides applied.

    This is the state a seeded newsroom is actually in, so it -- not the raw
    dump -- is what a classifier's qcodes have to conform to. Mirrors
    `add_vocabulary_items`, `repair_vocabulary_qcodes` and
    `repair_vocabulary_mojibake`, deliberately re-deriving from the same
    constants those functions read.
    """
    boot = bootstrap()
    vocabularies = {
        vocabulary_id: {**doc, "items": [dict(i) for i in (doc.get("items") or [])]}
        for vocabulary_id, doc in dumped_vocabularies().items()
    }

    for vocabulary_id, additions in boot.VOCABULARY_ITEM_ADDITIONS.items():
        vocabulary = vocabularies.get(vocabulary_id)
        if vocabulary is None:
            continue
        present = {item.get("qcode") for item in vocabulary["items"]}
        vocabulary["items"] += [
            dict(item) for item in additions if item["qcode"] not in present
        ]

    for vocabulary_id, name, wrong, right in boot.VOCABULARY_QCODE_REPAIRS:
        vocabulary = vocabularies.get(vocabulary_id)
        if vocabulary is None:
            continue
        for item in vocabulary["items"]:
            if item.get("name") == name and item.get("qcode") == wrong:
                item["qcode"] = right

    for vocabulary in vocabularies.values():
        for item in vocabulary["items"]:
            item["name"] = boot.demojibake(item.get("name"))

    return vocabularies


def qcodes(vocabulary_id):
    """The set of qcodes a seeded newsroom recognises for one vocabulary."""
    vocabulary = applied_vocabularies().get(vocabulary_id)
    if vocabulary is None:
        raise AssertionError(
            f"No vocabulary {vocabulary_id!r} in {CONTENT_CONFIG_ARCHIVE.name}. "
            "Either the dump was refreshed without it or the scheme name is wrong."
        )
    return {
        item["qcode"]
        for item in vocabulary["items"]
        if item.get("is_active", True) and item.get("qcode")
    }


def names_by_qcode(vocabulary_id):
    """{qcode: display name} for one vocabulary, mojibake already repaired."""
    return {
        item["qcode"]: item.get("name")
        for item in applied_vocabularies()[vocabulary_id]["items"]
        if item.get("qcode")
    }


def allowed_subject_schemes(profile_label):
    """The schemes a profile's `subject` validator will accept.

    Core validates every subject entry's `scheme` against this whitelist, so a
    classifier emitting a scheme that is not in it has its entries rejected --
    a different failure from an unknown qcode, and a louder one.
    """
    profile = content_profiles()[profile_label]
    subject = (profile.get("schema") or {}).get("subject") or {}
    scheme = ((subject.get("schema") or {}).get("schema") or {}).get("scheme") or {}
    return set(scheme.get("allowed") or [])
