# Illness groups: verified against the hospital data in IRIS (D50, D51)

**Status (Sat 17 Oct 2026, ~10:40, Nazar + Claude):** 20 illness groups, each with its own check-in
questionnaire, defined in `data/plans/groups-demo-v1.yaml` and built by
`uv run python -m hlidac.groups build`.

**Motol's agreement (D51):** the team (Nazar, 17 Oct 2026) reports that FN Motol verified the
approach and agreed that the team and its agents may read and write the hospital data in IRIS for
the demo, and that the team may write clinical content (group questions, red flags, recommendations)
for the demo. The content stays labelled DEMO; this is not a clinical sign-off of each item. The
check below still read **counts only**: no record, code list or patient id went into this repo.

## How a patient gets a group

`iris/demo_patients.py` reads the ICD-10 codes of each patient's hospital `Condition`s (read-only, in
memory) and `groups.group_for_codes` picks the group most of their codes fall in. A patient with no
matching code keeps the general questionnaire. ICD-10 blocks may not overlap (a test checks it), so
the choice never depends on file order.

Every group questionnaire = the general core items (fever, chills, unwell, nausea, vomiting,
diarrhoea; CTCAE-graded, UKONS rules) + the group's own yes/no items + free text. A "yes" to a group
item raises a routine review within 24 h (`group-symptom-reported`); a red-flag item is asked first and
goes through `red-flag-present`; "Nevím" or no answer is missing information. Nothing lowers urgency.

## Verification on IRIS (counts only)

325 patients have at least one ICD-10 cancer code; 73 have anticancer dose records (the demo patients).
**All 325 fall in a group. 72 of 73 dose patients fall in a group; the one left has no ICD-10
`Condition` at all and gets the general questionnaire.** Every group has patients.

| Group (`group_id`) | ICD-10 | Patients with the code | Dose patients |
|---|---|---:|---:|
| Nádory prsu (`breast`) | C50 | 39 | 3 |
| Nádory jícnu a žaludku (`upper_gi`) | C15–C16 | 16 | 4 |
| Nádory střeva, konečníku a řitě (`digestive`) | C17–C21 | 50 | 6 |
| Nádory jater, žlučových cest a slinivky (`liver_pancreas`) | C22–C25 | 13 | 6 |
| Nádory plic a hrudníku (`lung`) | C33–C39, C45, D38 | 18 | 5 |
| Nádory prostaty (`prostate`) | C61 | 8 | 3 |
| Nádory varlat a pyje (`testis`) | C60, C62–C63 | 8 | 1 |
| Nádory ledvin a močových cest (`urological`) | C64–C68 | 15 | 1 |
| Nádory děložního hrdla, pochvy a zevních rodidel (`gynaecological`) | C51–C53 | 18 | 1 |
| Nádory dělohy (`uterus`) | C54–C55, C58 | 8 | 2 |
| Nádory vaječníků a vejcovodů (`ovary`) | C56–C57 | 9 | 3 |
| Lymfomy (`haematological`) | C81–C88 | 24 | 4 |
| Leukémie a mnohočetný myelom (`leukaemia_myeloma`) | C90–C96 | 5 | 3 |
| Nádory hlavy a krku (`head_neck`) | C00–C14, C30–C32 | 28 | 6 |
| Nádory štítné žlázy a žláz s vnitřní sekrecí (`thyroid`) | C73–C75 | 8 | 2 |
| Nádory mozku, oka a nervové soustavy (`brain`) | C69–C72 | 8 | 1 |
| Melanom (`skin`) | C43 | 8 | 2 |
| Ostatní nádory kůže (`skin_other`) | C44 | 7 | 1 |
| Nádory kostí (`bone`) | C40–C41 | 16 | 9 |
| Sarkomy měkkých tkání a nádory nervů a pobřišnice (`sarcoma`) | C46–C49 | 19 | 9 |
| **Total** | | **325** | **72 (+1 general)** |

"Patients with the code" counts each patient once, in the group most of their codes fall in.

To repeat the check: `uv run python -m hlidac.iris.demo_patients --per-group 1` prints the counts per
questionnaire for the patients it seeds (it writes only our own SQL rows and tagged resources).

## Kept compatible

The ten D50 group ids keep their questionnaire URLs (some were narrowed: `digestive` is now the
bowel, `urological` the kidney and urinary tract, `gynaecological` the cervix, vagina and vulva,
`haematological` the lymphomas, `skin` melanoma, `sarcoma` the soft tissues), so plans already seeded
in IRIS stay valid. Re-seed with `--per-group 1` to give every demo patient their finer group.
