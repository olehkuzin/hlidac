"""Illness groups (D50): one check-in questionnaire per large cancer group.

`data/plans/groups-demo-v1.yaml` lists the groups, their ICD-10 blocks and their own items. Every
group questionnaire is the general demo Questionnaire's core items (so the CTCAE mapping and UKONS
rules apply unchanged) + the group's items + the free-text item, built by `build_questionnaire`.
The built files are versioned content in `data/`:

    uv run python -m hlidac.groups build   # (re)write data/questionnaires + data/plans group files
    uv run python -m hlidac.groups check   # fail if the files on disk differ from a fresh build

`group_for_codes` picks a patient's group from their ICD-10 codes (read from the hospital
`Condition`s in IRIS by `iris/demo_patients.py`, never stored by us). Pure apart from the CLI: no
IRIS, network or clock.
"""

import argparse
import copy
import json
import re
import sys
from functools import cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, StringConstraints, model_validator

from hlidac.content import DATA_DIR, read_yaml
from hlidac.schemas import Strict
from hlidac.schemas._base import Id, LinkId, NonEmpty, Version

GROUPS_FILE = DATA_DIR / "plans" / "groups-demo-v1.yaml"
CORE_QUESTIONNAIRE = "symptoms-demo-v1.json"
CORE_PLAN = "symptoms-demo-v1.plan.yaml"
FREE_TEXT_LINK_ID = "other"
DEMO_LABEL = "DEMO"
URL_PREFIX = "urn:hlidac:questionnaire:group-"

Icd10Block = Annotated[str, StringConstraints(pattern=r"^[A-Z][0-9]{2}(-[A-Z][0-9]{2})?$")]


class GroupItem(Strict):
    link_id: LinkId
    label_cs: NonEmpty  # short name for the nurse screens
    text: NonEmpty  # the approved Czech question
    red_flag: bool = False


class QuestionnaireRef(Strict):
    url: NonEmpty
    version: Version


class Group(Strict):
    group_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,30}$")]
    name_cs: NonEmpty
    icd10: Annotated[list[Icd10Block], Field(min_length=1)]
    items: Annotated[list[GroupItem], Field(min_length=1)]

    @property
    def questionnaire_url(self) -> str:
        return URL_PREFIX + self.group_id.replace("_", "-")

    @property
    def file_stem(self) -> str:
        return f"symptoms-group-{self.group_id.replace('_', '-')}-v1"

    def matches(self, code: str) -> bool:
        """True if the ICD-10 code (e.g. 'C50.9' or 'C509') falls in one of the group's blocks."""
        head = code.strip().upper()[:3]
        if not re.fullmatch(r"[A-Z][0-9]{2}", head):
            return False
        for block in self.icd10:
            low, _, high = block.partition("-")
            if low <= head <= (high or low):
                return True
        return False


class Groups(Strict):
    content_id: Id
    version: Version
    approval: NonEmpty
    approved_by: Id | None
    source_ref: NonEmpty
    fallback_questionnaire: QuestionnaireRef
    groups: Annotated[list[Group], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> "Groups":
        if self.approved_by is None and DEMO_LABEL not in self.approval:
            raise ValueError("unapproved groups are labelled DEMO")
        ids = [g.group_id for g in self.groups]
        if len(ids) != len(set(ids)):
            raise ValueError("group ids are unique")
        for group in self.groups:
            links = [i.link_id for i in group.items]
            if len(links) != len(set(links)):
                raise ValueError(f"{group.group_id}: item link ids are unique")
        # A code may fall in one group only, so the assignment never depends on file order.
        blocks = [(g.group_id, b) for g in self.groups for b in g.icd10]
        for n, (gid, block) in enumerate(blocks):
            low, _, high = block.partition("-")
            high = high or low
            if low > high:
                raise ValueError(f"{gid}: ICD-10 block {block} is reversed")
            for other_gid, other in blocks[n + 1 :]:
                o_low, _, o_high = other.partition("-")
                if not (high < o_low or (o_high or o_low) < low):
                    raise ValueError(f"{gid} and {other_gid}: ICD-10 blocks overlap")
        return self

    def by_url(self, url: str) -> Group | None:
        return next((g for g in self.groups if g.questionnaire_url == url), None)

    def labels(self) -> dict[str, str]:
        """link id → short Czech name, for every group item."""
        return {i.link_id: i.label_cs for g in self.groups for i in g.items}


def read_groups(path: Path = GROUPS_FILE) -> Groups:
    try:
        return Groups.model_validate(read_yaml(path))
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: {error}") from error


@cache
def default_groups() -> Groups:
    return read_groups()


def group_for_codes(groups: Groups, codes: list[str]) -> Group | None:
    """The patient's group: the one most of their cancer codes fall in (ties by file order), or
    None if no code matches (the general questionnaire then applies)."""
    counts = {g.group_id: sum(g.matches(c) for c in codes) for g in groups.groups}
    best = max(groups.groups, key=lambda g: counts[g.group_id])
    return best if counts[best.group_id] > 0 else None


# --- building the questionnaires --------------------------------------------------------------


def _yes_no_item(item: GroupItem) -> dict[str, Any]:
    built: dict[str, Any] = {
        "linkId": item.link_id,
        "text": item.text,
        "type": "choice",
        "required": True,
        "code": [
            {
                "system": "urn:hlidac:symptom",
                "code": item.link_id.replace("_", "-"),
                "display": item.label_cs[:1].upper() + item.label_cs[1:],
            }
        ],
    }
    if item.red_flag:
        built["extension"] = [{"url": "urn:hlidac:extension:red-flag", "valueBoolean": True}]
    built["answerOption"] = [
        {"valueCoding": {"system": "urn:hlidac:answer", "code": code, "display": display}}
        for code, display in (("yes", "Ano"), ("no", "Ne"), ("dont_know", "Nevím"))
    ]
    return built


def build_questionnaire(core: dict[str, Any], group: Group, groups: Groups) -> dict[str, Any]:
    """The group's Questionnaire: core items, then the group's items, then the free-text item."""
    core_items = [i for i in core["item"] if i["linkId"] != FREE_TEXT_LINK_ID]
    free_text = [i for i in core["item"] if i["linkId"] == FREE_TEXT_LINK_ID]
    clash = {i["linkId"] for i in core["item"]} & {i.link_id for i in group.items}
    if clash:
        raise ValueError(f"{group.group_id}: items clash with the core: {sorted(clash)}")
    q = copy.deepcopy(core)
    slug = group.group_id.replace("_", "-")
    q["url"] = group.questionnaire_url
    q["identifier"] = [{"system": "urn:hlidac:questionnaire", "value": f"group-{slug}-v1"}]
    q["version"] = "1"
    q["name"] = "HlidacSymptoms" + "".join(p.title() for p in group.group_id.split("_"))
    q["description"] = (
        f"DEMO — čeká na schválení FN Motol. Skupina: {group.name_cs}. Společné otázky "
        "z obecného dotazníku (horečka, zimnice, celkově zle, nevolnost, zvracení, průjem) "
        "a otázky pro tuto skupinu onemocnění. Zdroj: data/plans/groups-demo-v1.yaml "
        f"(verze {groups.version})."
    )
    q["item"] = [*copy.deepcopy(core_items), *(_yes_no_item(i) for i in group.items),
                 *copy.deepcopy(free_text)]  # fmt: skip
    return q


def build_sidecar(core_sidecar: dict[str, Any], group: Group, groups: Groups) -> dict[str, Any]:
    sidecar = copy.deepcopy(core_sidecar)
    sidecar["questionnaire"] = f"{group.file_stem}.json"
    sidecar["source_ref"] = (
        f"Group '{group.name_cs}' ({', '.join(group.icd10)}) from data/plans/groups-demo-v1.yaml"
        f" version {groups.version}: {groups.source_ref.strip()} Core items: "
        + str(core_sidecar["source_ref"]).strip()
    )
    return sidecar


def build_files(data_dir: Path = DATA_DIR) -> dict[Path, str]:
    """Every group file's path → its exact content."""
    groups = read_groups(data_dir / "plans" / GROUPS_FILE.name)
    core = json.loads((data_dir / "questionnaires" / CORE_QUESTIONNAIRE).read_text("utf-8"))
    core_sidecar = read_yaml(data_dir / "plans" / CORE_PLAN)
    files: dict[Path, str] = {}
    for group in groups.groups:
        q = build_questionnaire(core, group, groups)
        files[data_dir / "questionnaires" / f"{group.file_stem}.json"] = (
            json.dumps(q, ensure_ascii=False, indent=2) + "\n"
        )
        sidecar = build_sidecar(core_sidecar, group, groups)
        header = (
            f"# GENERATED by `uv run python -m hlidac.groups build` from groups-demo-v1.yaml "
            f"(group {group.group_id}); do not edit by hand. No patient data.\n"
        )
        files[data_dir / "plans" / f"{group.file_stem}.plan.yaml"] = header + yaml.safe_dump(
            sidecar, allow_unicode=True, sort_keys=False, width=100
        )
    return files


def group_questionnaire_refs(groups: Groups) -> list[tuple[str, str]]:
    return [(g.questionnaire_url, "1") for g in groups.groups]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hlidac.groups")
    parser.add_argument("command", choices=["build", "check"])
    args = parser.parse_args(argv)
    files = build_files()
    if args.command == "build":
        for path, text in files.items():
            path.write_text(text, encoding="utf-8")
        print(f"wrote {len(files)} files for {len(files) // 2} groups")
        return 0
    stale = [p.name for p, text in files.items() if not p.exists() or p.read_text("utf-8") != text]
    if stale:
        print(f"out of date: {', '.join(stale)} (run build)", file=sys.stderr)
        return 1
    print(f"{len(files) // 2} group questionnaires up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
